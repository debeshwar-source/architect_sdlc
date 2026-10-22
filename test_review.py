"""Checks of the pull-request reviewer (review/). Run: pytest test_review.py

The AI and GitHub are never called: the AI is replaced by a fake answer, GitHub by a fake request function.
"""

import pytest

import llm
from coding import agent as coding_agent
from coding import diffview
from review import github, loop, reviewer

DIFF = """diff --git a/app.py b/app.py
index 111..222 100644
--- a/app.py
+++ b/app.py
@@ -1,5 +1,6 @@
 import os
 
 def total(items):
-    return sum(items)
+    result = 0
+    for i in range(len(items) - 1):
+        result += items[i]
+    return result
 
diff --git a/gone.py b/gone.py
deleted file mode 100644
index 333..000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
"""
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing here may reach GitHub: a test that needs an answer sets its own."""
    def offline(*a, **k):
        raise github.GithubError("offline in tests")
    monkeypatch.setattr(github, "_request", offline)


@pytest.fixture(autouse=True)
def _no_checkout(monkeypatch):
    """No real clone either: the reviewer falls back to the diff unless a test provides a checkout."""
    from review import checkout

    def offline(*a, **k):
        raise checkout.CheckoutError("offline in tests")
    monkeypatch.setattr(checkout, "prepare", offline)


@pytest.fixture(autouse=True)
def _restore_modules():
    """The screen tests swap functions on the real modules from inside their scripts; put every one back after each test."""
    import review.repos as repos_mod
    from review import checkout as checkout_mod
    mods = [github, loop, repos_mod, llm, reviewer, checkout_mod, coding_agent]
    saved = [(m, dict(vars(m))) for m in mods]
    yield
    for m, attrs in saved:
        for k, v in attrs.items():
            setattr(m, k, v)


from review import checkout as _checkout_module
REAL_PREPARE = _checkout_module.prepare                      # the real one, captured before any test stubs it


PR = {"number": 7, "title": "Add total", "author": "bot", "branch": "feat", "base": "main", "body": "adds total", "head_sha": "abc"}


def fake_llm(monkeypatch, comments, summary="Needs fixes."):
    seen = {}

    def fake(prompt, agent_name, required_keys=None, system=None, model=None, validate=None, **kw):
        seen["prompt"] = prompt
        answer = {"summary": summary, "comments": comments}
        validate(answer)
        return answer
    monkeypatch.setattr(llm, "call_llm_json", fake)
    return seen


def test_commentable_lines_skip_deleted_files():
    lines = reviewer.commentable(diffview.parse(DIFF))
    assert set(lines) == {"app.py"}
    assert lines["app.py"][5] == "    for i in range(len(items) - 1):"


def test_review_anchors_and_snaps_comments(monkeypatch):
    seen = fake_llm(monkeypatch, [
        {"path": "app.py", "line": 5, "severity": "blocker", "comment": "Skips the last item.", "suggestion_code": "    for i in range(len(items)):"},
        {"path": "app.py", "line": 8, "severity": "bogus", "comment": "Near the end.", "suggestion_code": ""},   # past the diff -> snapped
        {"path": "app.py", "line": 99, "severity": "nit", "comment": "Far away.", "suggestion_code": ""},       # dropped
        {"path": "other.py", "line": 1, "severity": "nit", "comment": "Not in the PR.", "suggestion_code": ""},  # dropped
    ])
    out = reviewer.review(PR, DIFF)
    assert "L5|" in seen["prompt"] and "gone.py" not in seen["prompt"]
    assert [(c["path"], c["line"], c["severity"]) for c in out["comments"]] == [("app.py", 5, "blocker"), ("app.py", 8, "suggestion")]
    assert "```suggestion\n    for i in range(len(items)):\n```" in out["comments"][0]["body"]
    assert any("2 comment(s)" in n for n in out["notes"])


def test_empty_diff_needs_no_ai(monkeypatch):
    monkeypatch.setattr(llm, "call_llm_json", lambda *a, **k: pytest.fail("AI should not be called"))
    assert reviewer.review(PR, "")["comments"] == []


def test_context_snippet_shows_lines_above():
    f = diffview.parse(DIFF)[0]
    text = reviewer.context_snippet(f, 5)
    assert "-  " in text and "return sum(items)" in text        # the removed line is shown
    assert "    4 +  " in text and "result = 0" in text
    assert text.rstrip().endswith("for i in range(len(items) - 1):")


@pytest.mark.parametrize("text,expected", [
    ("octo/hello", ("octo", "hello")),
    ("https://github.com/octo/hello.git", ("octo", "hello")),
    ("git@github.com:octo/hello.git", ("octo", "hello")),
    ("nonsense", None),
])
def test_parse_repo(text, expected):
    assert github.parse_repo(text) == expected


def test_list_prs_hides_drafts_and_post_review_payload(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, accept=""):
        calls.append((method, path, body))
        if method == "GET":
            base = {"number": 1, "title": "t", "user": {"login": "u"}, "head": {"ref": "b", "sha": "s"}, "base": {"ref": "main"}, "html_url": "x"}
            return [dict(base), dict(base, number=2, draft=True)]
        return {"html_url": "https://github.com/o/r/pull/1#pullrequestreview-9"}      # no review id: no comment lookup
    monkeypatch.setattr(github, "_request", fake_request)
    assert [p["number"] for p in github.list_open_prs("o", "r")] == [1]
    out = github.post_review("o", "r", 1, "s", "sum", "COMMENT", [{"path": "a.py", "line": 3, "body": "hi"}])
    assert out["url"].endswith("pullrequestreview-9") and out["comments"] == []
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/pulls/1/reviews")
    assert body["commit_id"] == "s" and body["comments"] == [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "hi"}]
    with pytest.raises(github.GithubError):
        github.post_review("o", "r", 1, "s", "", "APPROVE", [])


def test_screen_lists_prs_reviews_and_posts_edited_comment(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    posted = {}

    def script():
        import llm
        from review import github, ui
        import test_review as t
        import review.repos as _rp; _rp.known_repos = lambda: ["octo/hello"]
        github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="")]
        github.get_diff = lambda o, n, num: t.DIFF
        github.get_pr = lambda o, n, num: dict(t.PR)
        github.post_review = lambda *a: (t.posted.update(args=a), {"url": "https://x/review", "comments": []})[1]
        llm.call_llm_json = lambda *a, **k: {"summary": "S", "comments": [
            {"path": "app.py", "line": 5, "severity": "blocker", "comment": "Skips last.", "suggestion_code": ""}]}
        ui.render()

    import test_review
    test_review.posted = posted
    at = AppTest.from_function(script, default_timeout=30).run()
    at.text_input[0].set_value("octo/hello").run()
    assert "#7" in at.selectbox[0].options[0]
    at.button(key="rv_go").click().run()
    assert not at.exception
    body_key = [t.key for t in at.text_area if t.key and "body_" in t.key][0]
    at.text_area(key=body_key).set_value("Reworded by a human").run()
    at.button(key=[b.key for b in at.button if b.key and b.key.endswith("post")][0]).click().run()
    assert not at.exception
    args = posted["args"]
    assert args[:4] == ("octo", "hello", 7, "abc") and args[4] == "S" and args[5] == "COMMENT"
    assert [(c["path"], c["line"], c["body"]) for c in args[6]] == [("app.py", 5, "Reworded by a human")]
    assert any("Posted 1 comment and the summary on pull request #7" in m.value and "https://x/review" in m.value for m in at.success), \
        "the person is told the comments were pushed, right after approving"           # (the leading ✅ is shown as the box's icon)
    assert loop.load("octo", "hello", 7)["comments"][0]["body"] == "Reworded by a human"           # the conversation begins from the posted review


# ---------- the conversation between the coding agent and the reviewer ----------

import subprocess

from coding import workspace as ws
from review import fixer, loop


def sh(cwd, *args):
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def convo(tmp_path, monkeypatch):
    """A real remote and clone with a PR branch, a saved first review (c1, c2), and GitHub recorded instead of called."""
    remote, tree = tmp_path / "remote.git", tmp_path / "tree"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    sh(tmp_path, "clone", "-q", str(remote), str(tree))
    (tree / "app.py").write_text("def total(items):\n    return sum(items[:-1])\n")
    sh(tree, "add", "-A"); sh(tree, "commit", "-q", "-m", "init"); sh(tree, "push", "-q", "origin", "HEAD:main")
    sh(tree, "checkout", "-q", "-b", "feat")
    (tree / "app.py").write_text("def total(items):\n    return sum(items[:-1])\n\ndef avg(items):\n    return total(items) / len(items)\n")
    sh(tree, "add", "-A"); sh(tree, "commit", "-q", "-m", "add avg"); sh(tree, "push", "-q", "-u", "origin", "feat")
    base = ws.git(tree, "rev-parse", "main")
    w = ws.Workspace(tree, tree, "feat", "main", base, str(remote))
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setattr(loop, "open_workspace", lambda state: w)
    gh = {"replies": [], "resolved": [], "reviews": [], "deleted": [], "reopened": []}
    monkeypatch.setattr(github, "reply", lambda o, n, num, cid, body: (gh["replies"].append((cid, body)), 500 + len(gh["replies"]))[1])
    monkeypatch.setattr(github, "delete_comment", lambda o, n, cid: gh["deleted"].append(cid))

    def fake_resolve(o, n, num, ids, resolved=True):
        gh["resolved" if resolved else "reopened"].extend(ids)
        return list(ids)
    monkeypatch.setattr(github, "resolve_threads", fake_resolve)
    monkeypatch.setattr(github, "post_review", lambda o, n, num, sha, body, ev, cs: (gh["reviews"].append(cs), {"url": "u", "comments": [
        {"path": c["path"], "line": c["line"], "gh_id": 900 + i, "body": c["body"]} for i, c in enumerate(cs)]})[1])
    pr = dict(PR, head_repo="o/r", base_repo="o/r", head_sha=ws.head(w))
    posted = {"url": "u", "comments": [{"path": "app.py", "line": 2, "gh_id": 11, "body": ""}, {"path": "app.py", "line": 5, "gh_id": 12, "body": ""}]}
    chosen = [{"id": "c1", "path": "app.py", "line": 2, "severity": "blocker", "body": "Drops the last item."},
              {"id": "c2", "path": "app.py", "line": 5, "severity": "suggestion", "body": "Divides by zero on empty input."}]
    return {"tree": tree, "remote": remote, "gh": gh, "state": loop.start("o", "r", pr, posted, chosen), "w": w}


def fake_coder(monkeypatch, tree, outcomes):
    def fake(pr, comments, cwd, guidance="", model=None, resume=None, on_event=None):
        (tree / "app.py").write_text("def total(items):\n    return sum(items)\n\ndef avg(items):\n    return total(items) / len(items)\n")
        return {"answer": fixer.clean({"summary": "Fixed the slice.", "outcomes": outcomes}, comments), "session_id": "sess-1"}
    monkeypatch.setattr(fixer, "run", fake)


def test_start_ties_each_comment_to_its_github_thread(convo):
    assert [(c["id"], c["gh_id"], c["status"]) for c in convo["state"]["comments"]] == [("c1", 11, "open"), ("c2", 12, "open")]
    assert convo["state"]["phase"] == "awaiting_fix" and loop.load("o", "r", 7)["comments"][0]["gh_id"] == 11


def test_a_full_round_fix_push_reply_recheck_resolve(convo, monkeypatch):
    state, gh = convo["state"], convo["gh"]
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "Removed the slice."},
                                           {"id": "c2", "outcome": "needs_input", "reply": "Should empty input return 0 or raise?"}])
    loop.run_fix(state, "", None)
    assert "return sum(items)" in loop.fix_diff(state)

    loop.approve_fix(state, {"c1": "Removed the slice (edited by a human)."}, "")
    assert gh["replies"] == [(11, loop.CODER_TAG + "Removed the slice (edited by a human).")]          # needs_input is never posted
    assert [c["status"] for c in state["comments"]] == ["fixed", "open"] and state["round"] == 0 and state["fix"] is None
    pushed = subprocess.run(["git", "log", "--format=%s", "feat"], cwd=convo["remote"], capture_output=True, text=True).stdout.split()
    assert pushed[:3] == ["Address", "review", "comments"]                                         # the branch on the remote moved

    new_head = ws.head(convo["w"])
    monkeypatch.setattr(github, "get_pr", lambda *a: dict(PR, head_sha=new_head))
    monkeypatch.setattr(github, "compare_diff", lambda *a: DIFF)
    monkeypatch.setattr(github, "get_diff", lambda *a: DIFF)
    monkeypatch.setattr(llm, "call_llm_json", lambda *a, **k: {"summary": "Mostly settled.", "verdicts": [
        {"id": "c1", "resolved": True, "reply": "Fixed, thanks."}], "comments": [
        {"path": "app.py", "line": 6, "severity": "nit", "comment": "Name it better.", "suggestion_code": ""}]})
    loop.run_recheck(state)
    out = state["recheck"]
    assert [(v["id"], v["resolved"]) for v in out["verdicts"]] == [("c1", True)] and out["comments"][0]["id"] == "c3"

    loop.approve_recheck(state, {"c1": True}, {"c1": "Fixed, thanks."}, out["comments"], "ok")
    assert gh["resolved"] == [11] and gh["replies"][-1] == (11, loop.REVIEWER_TAG + "Fixed, thanks.")
    assert [(c["id"], c["status"]) for c in state["comments"]] == [("c1", "resolved"), ("c2", "open"), ("c3", "open")]
    assert state["comments"][2]["gh_id"] == 900 and state["phase"] == "awaiting_fix" and state["recheck"] is None
    assert loop.load("o", "r", 7)["round"] == 1                                                      # saved: survives a restart


def test_disagreement_goes_to_the_reviewer_and_everything_resolved_means_ready(convo, monkeypatch):
    state = convo["state"]
    state["comments"][0]["status"] = state["comments"][1]["status"] = "resolved"
    loop._settle(state)
    assert state["phase"] == "approved"


def test_there_is_no_round_limit_because_every_step_needs_the_persons_approval(convo, monkeypatch):
    state = convo["state"]
    state["round"] = 25
    loop._settle(state)
    assert state["phase"] == "awaiting_fix"
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "ok"}, {"id": "c2", "outcome": "fixed", "reply": "ok"}])
    loop.run_fix(state, "", None)
    assert state["fix"] is not None


def test_fork_prs_are_refused(tmp_path, monkeypatch):
    monkeypatch.undo()
    with pytest.raises(loop.LoopError, match="fork"):
        loop.open_workspace({"repo": ["o", "r"], "branch": "x", "number": 1, "base": "main", "head_repo": "someone/r", "base_repo": "o/r"})


def test_unjudged_thread_is_never_closed_by_silence(monkeypatch):
    monkeypatch.setattr(llm, "call_llm_json", lambda *a, **k: {"summary": "s", "verdicts": [], "comments": []})
    out = reviewer.recheck(PR, [{"id": "c1", "path": "app.py", "line": 5, "body": "b", "thread": []}], DIFF, DIFF, 2)
    assert out["verdicts"] == [{"id": "c1", "resolved": False, "reply": "Not re-checked; still open."}]


def test_fixer_never_drops_a_comment_and_picks_an_expert():
    cleaned = fixer.clean({"summary": "x", "outcomes": [{"id": "c1", "outcome": "fixed", "reply": " done  "}]},
                          [{"id": "c1"}, {"id": "c2"}])
    assert [(o["id"], o["outcome"]) for o in cleaned["outcomes"]] == [("c1", "fixed"), ("c2", "needs_input")]
    assert fixer.pick_role(["a/B.java", "c/D.java"]) == "coding_java" and fixer.pick_role(["README"]) == "coding_python"


@pytest.mark.parametrize("stage", ["open", "fix", "fix_failing", "recheck", "approved"])
def test_conversation_screen_renders_every_stage(stage, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import json
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    cm = lambda i, st: {"id": f"c{i}", "gh_id": 10 + i, "path": "app.py", "line": 5, "severity": "nit", "body": f"body {i}", "status": st,
                        "thread": [{"who": "coder", "text": "did it"}] if st != "open" else []}
    state = {"repo": ["o", "r"], "number": 7, "title": "t", "branch": "feat", "base": "main", "head_repo": "o/r", "base_repo": "o/r",
             "head_sha": "abc", "round": 1, "comments": [cm(1, "open"), cm(2, "fixed")], "session_id": None, "fix": None, "recheck": None,
             "error": None, "notes": []}
    if stage in ("fix", "fix_failing"):
        state["fix"] = {"before": "x", "summary": "Changed it.", "targets": ["c1"], "tests": {"command": "python3 -m pytest -q", "report": None, "note": "none"},
                        "outcomes": [{"id": "c1", "outcome": "fixed", "reply": "ok"}, {"id": "c2", "outcome": "needs_input", "reply": "Which one?"}]}
    if stage == "fix_failing":
        state["fix"]["tests"]["report"] = {"ran": True, "total": 3, "passed": 2, "failed": 1, "skipped": 0, "build_failed": False, "tail": "boom",
                                           "failed_cases": [{"name": "test_it", "message": "assert False"}]}
    if stage == "recheck":
        state["recheck"] = {"summary": "s", "new_head": "n", "notes": [], "verdicts": [{"id": "c2", "resolved": True, "reply": "Thanks"}],
                            "comments": [{"id": "c3", "path": "app.py", "line": 6, "severity": "nit", "body": "new one"}]}
    if stage == "approved":
        state["comments"] = [cm(1, "resolved")]
    state["actions"] = [{"kind": "review", "label": "the review (its comments)", "at": "10:00:00", "snapshot": None, "undo": {"comment_ids": [11]}},
                        {"kind": "fix", "label": "the coding agent's fixes (round 1)", "at": "10:05:00", "snapshot": {}, "undo": {"reply_ids": [], "revert": None}}]
    loop._settle(state)
    loop.STORE.mkdir(parents=True)
    (loop.STORE / "o__r__7.json").write_text(json.dumps(state))
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    def script():
        from review import github, loop, ui
        import test_review as t
        loop.STORE = t.STORE_FOR_TEST
        import review.repos as _rp; _rp.known_repos = lambda: ["o/r"]
        github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="")]
        loop.fix_diff = lambda s: t.DIFF
        ui.render()

    import test_review
    test_review.STORE_FOR_TEST = loop.STORE
    at = AppTest.from_function(script, default_timeout=30).run()
    at.text_input[0].set_value("o/r").run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Conversation on #7" in text
    labels = " ".join(b.label for b in at.button)
    undo = {b.label: b.disabled for b in at.button if b.label.startswith("↶ Undo")}
    assert undo["↶ Undo the coding agent's fixes (round 1)"] is True                  # disabled until its "remove from GitHub" tick
    assert undo["↶ Undo the review (its comments)"] is True                           # not the newest: the newer one first
    assert ("↶ Discard the coding agent's change" in labels) == (stage in ("fix", "fix_failing"))
    assert ("↶ Discard the reviewer's re-check" in labels) == (stage == "recheck")
    if stage == "open":                                                  # c2 is answered while c1 is still open: both next steps are offered
        assert "🔍 Ask the reviewer to review the 1 selected again" in [b.label for b in at.button]
    assert {"open": "Let the coding agent address all 1 open", "fix": "Approve: commit, push", "fix_failing": "Approve: commit, push",
            "recheck": "Approve: post the replies", "approved": "Start over"}[stage] in labels or stage == "approved"
    per_comment = [b.label for b in at.button if b.label in ("🛠️ Ask the coding agent", "Skip", "💬 Respond & resolve")]
    if stage == "open":      # c1 is open (all three choices), c2 is answered by the coding agent (only Respond & resolve)
        assert sorted(per_comment) == sorted(["Skip", "🛠️ Ask the coding agent", "💬 Respond & resolve", "💬 Respond & resolve"])
    elif stage != "approved":
        assert per_comment == [], "per-comment buttons only while nothing waits for approval"
    if stage in ("fix", "fix_failing"):
        approve = next(b for b in at.button if b.label.startswith("✅ Approve: commit"))
        assert approve.disabled is (stage == "fix_failing"), "failing tests block approval"
        assert any("anyway" in c.label for c in at.checkbox) == (stage == "fix_failing")
        assert any("tests fail" in e.value or "Total 3 tests" in e.value for e in at.error) == (stage == "fix_failing")
    if stage == "approved":
        assert "Every comment is settled" in " ".join(s.value for s in at.success)


def test_dropdowns_are_closed_until_opened_and_review_is_lazy(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    def script():
        import streamlit as st
        from ui_kit import dropdown
        drawn = st.session_state.setdefault("drawn", [])
        dropdown("Eager", "dd_a", lambda: (drawn.append("a"), st.text_input("typed", key="a_in")))
        dropdown("Lazy", "dd_b", lambda: (drawn.append("b"), st.write("lazy body")), lazy=True)

    at = AppTest.from_function(script, default_timeout=20).run()
    assert at.session_state["drawn"] == ["a"]                                        # the lazy one did not run at all
    assert "display:none" in " ".join(m.value for m in at.markdown)                  # the eager one is hidden while closed
    assert [b.label for b in at.button] == ["▸  Eager", "▸  Lazy"]
    at.button(key="dd_a_head").click().run()
    assert at.button(key="dd_a_head").label == "▾  Eager" and "display:none" not in " ".join(m.value for m in at.markdown)
    at.text_input(key="a_in").set_value("kept").run()
    at.button(key="dd_a_head").click().run()                                         # close again: what was typed is still there
    assert at.text_input(key="a_in").value == "kept"
    at.button(key="dd_b_head").click().run()
    assert at.session_state["drawn"].count("b") == 1


# ---------- one undo per task (reviewer side) ----------

def _round_one(convo, monkeypatch):
    """The coding agent fixes c1 and answers c2 with a question's opposite (disagrees); the reviewer resolves c1 and adds a new comment."""
    state = convo["state"]
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "Removed the slice."},
                                           {"id": "c2", "outcome": "disagree", "reply": "Empty input is rejected upstream."}])
    loop.run_fix(state, "", None)
    loop.approve_fix(state, {}, "")
    new_head = ws.head(convo["w"])
    monkeypatch.setattr(github, "get_pr", lambda *a: dict(PR, head_sha=new_head))
    monkeypatch.setattr(github, "compare_diff", lambda *a: DIFF)
    monkeypatch.setattr(github, "get_diff", lambda *a: DIFF)
    monkeypatch.setattr(llm, "call_llm_json", lambda *a, **k: {"summary": "s", "verdicts": [
        {"id": "c1", "resolved": True, "reply": "Fixed."}, {"id": "c2", "resolved": False, "reply": "Still not convinced."}], "comments": [
        {"path": "app.py", "line": 6, "severity": "nit", "comment": "Name it better.", "suggestion_code": ""}]})
    loop.run_recheck(state)
    loop.approve_recheck(state, {}, {}, state["recheck"]["comments"], "s")
    return state


def test_each_action_is_listed_newest_first_and_only_the_newest_can_be_undone(convo, monkeypatch):
    state = _round_one(convo, monkeypatch)
    assert [a["kind"] for a in loop.actions(state)] == ["recheck", "fix", "review"]
    assert loop.undo_blocker(state) == ""
    state["fix"] = {"before": "x"}
    assert "waiting for your approval" in loop.undo_blocker(state)


def test_undo_the_recheck_deletes_its_posts_reopens_threads_and_restores_the_comments(convo, monkeypatch):
    state, gh = _round_one(convo, monkeypatch), convo["gh"]
    assert [c["status"] for c in state["comments"]] == ["resolved", "open", "open"]
    reply_ids = [m["gh_id"] for c in state["comments"] for m in c["thread"] if m["who"] == "reviewer"]
    loop.undo_last(state)
    assert gh["reopened"] == [11] and set(gh["deleted"]) == set(reply_ids) | {900}              # its replies and the new comment are gone
    assert [(c["id"], c["status"]) for c in state["comments"]] == [("c1", "fixed"), ("c2", "disputed")]
    assert all(m["who"] == "coder" for c in state["comments"] for m in c["thread"])
    assert [a["kind"] for a in loop.actions(state)] == ["fix", "review"] and state["phase"] == "awaiting_recheck"
    assert loop.load("o", "r", 7)["comments"][0]["status"] == "fixed"                              # saved


def test_undo_the_fix_reverts_the_push_with_a_new_commit_and_deletes_the_replies(convo, monkeypatch):
    state, gh = convo["state"], convo["gh"]
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "Removed the slice."},
                                           {"id": "c2", "outcome": "disagree", "reply": "No."}])
    loop.run_fix(state, "", None)
    loop.approve_fix(state, {}, "")
    assert "sum(items)\n" in (convo["tree"] / "app.py").read_text()
    loop.undo_last(state)
    assert "items[:-1]" in (convo["tree"] / "app.py").read_text()                                    # the bug is back, as it was
    log = subprocess.run(["git", "log", "--format=%s", "feat"], cwd=convo["remote"], capture_output=True, text=True).stdout.splitlines()
    assert log[0].startswith("Revert") and log[1] == "Address review comments (round 1)"             # history kept: undone by a new commit
    assert set(gh["deleted"]) == {501, 502} and state["round"] == 0 and state["session_id"] is None
    assert [c["status"] for c in state["comments"]] == ["open", "open"] and state["phase"] == "awaiting_fix"


def test_undo_the_fix_refuses_when_someone_else_pushed_meanwhile(convo, monkeypatch, tmp_path):
    state = convo["state"]
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "ok"}, {"id": "c2", "outcome": "disagree", "reply": "no"}])
    loop.run_fix(state, "", None)
    loop.approve_fix(state, {}, "")
    other = tmp_path / "other"
    sh(tmp_path, "clone", "-q", "-b", "feat", str(convo["remote"]), str(other))
    (other / "x.txt").write_text("x"); sh(other, "add", "-A"); sh(other, "commit", "-q", "-m", "someone else"); sh(other, "push", "-q")
    with pytest.raises(loop.LoopError, match="new commits"):
        loop.undo_last(state)
    assert convo["gh"]["deleted"] == [] and len(state["actions"]) == 2                                # nothing was touched


def test_undo_the_review_deletes_its_comments_and_returns_to_the_start(convo):
    loop.undo_last(convo["state"])
    assert sorted(convo["gh"]["deleted"]) == [11, 12] and loop.load("o", "r", 7) is None


def test_a_failed_delete_stops_before_anything_is_forgotten_and_a_retry_finishes(convo, monkeypatch):
    calls = {"n": 0}

    def flaky(o, n, cid):
        calls["n"] += 1
        if calls["n"] == 2:
            raise github.GithubError("boom", 500)
        convo["gh"]["deleted"].append(cid)
    monkeypatch.setattr(github, "delete_comment", flaky)
    with pytest.raises(loop.LoopError, match="try again"):
        loop.undo_last(convo["state"])
    assert loop.load("o", "r", 7) is not None                                                         # the conversation is still there
    loop.undo_last(convo["state"])
    assert loop.load("o", "r", 7) is None


def test_discarding_pending_work_changes_nothing_on_github(convo, monkeypatch):
    state = convo["state"]
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "ok"}, {"id": "c2", "outcome": "disagree", "reply": "no"}])
    loop.run_fix(state, "", None)
    assert "sum(items)\n" in (convo["tree"] / "app.py").read_text() and state["session_id"] == "sess-1"
    loop.discard_fix(state)
    assert "items[:-1]" in (convo["tree"] / "app.py").read_text() and state["fix"] is None and state["session_id"] is None
    assert convo["gh"]["replies"] == [] and convo["gh"]["deleted"] == []
    with pytest.raises(loop.LoopError):
        loop.discard_fix(state)
    state["recheck"] = {"verdicts": []}
    loop.discard_recheck(state)
    assert state["recheck"] is None


def test_delete_comment_tolerates_already_gone_but_not_other_errors(monkeypatch):
    def gone(*a, **k): raise github.GithubError("nope", 404)
    monkeypatch.setattr(github, "_request", gone)
    github.delete_comment("o", "r", 1)
    def denied(*a, **k): raise github.GithubError("nope", 403)
    monkeypatch.setattr(github, "_request", denied)
    with pytest.raises(github.GithubError):
        github.delete_comment("o", "r", 1)


# ---------- fix one comment at a time, skip, and tests before approval ----------

def fake_coder_with_test(monkeypatch, tree, passing):
    """A coder that also writes a unit test; it passes or fails for real when the program runs it."""
    def fake(pr, comments, cwd, guidance="", model=None, resume=None, on_event=None):
        (tree / "app.py").write_text("def total(items):\n    return sum(items)\n")
        (tree / "test_app.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_it(self):\n"
                                          f"        self.assertTrue({passing})\n\nif __name__ == '__main__':\n    unittest.main()\n")
        outcomes = [{"id": c["id"], "outcome": "fixed", "reply": "done"} for c in comments]
        return {"answer": fixer.clean({"summary": "s", "outcomes": outcomes, "test_command": "python3 -m unittest -v"}, comments), "session_id": "s"}
    monkeypatch.setattr(fixer, "run", fake)


def test_fixing_one_comment_leaves_the_others_open_and_does_not_use_up_a_round(convo, monkeypatch):
    state = convo["state"]
    fake_coder(monkeypatch, convo["tree"], [{"id": "c2", "outcome": "fixed", "reply": "Guarded the empty case."}])
    loop.run_fix(state, "", None, only=["c2"])
    assert state["fix"]["targets"] == ["c2"] and [o["id"] for o in state["fix"]["outcomes"]] == ["c2"]
    loop.approve_fix(state, {}, "")
    assert [(c["id"], c["status"]) for c in state["comments"]] == [("c1", "open"), ("c2", "fixed")]
    assert state["round"] == 0 and state["phase"] == "awaiting_fix"                       # c1 is still the coding agent's to do
    assert [r[0] for r in convo["gh"]["replies"]] == [12]                                   # only c2's thread got a reply
    with pytest.raises(loop.LoopError):
        loop.run_fix(state, "", None, only=["c2"])                                          # c2 is no longer open


def test_asking_again_keeps_the_same_comments(convo, monkeypatch):
    state = convo["state"]
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "x"}])
    loop.run_fix(state, "", None, only=["c1"])
    loop.run_fix(state, "use a different approach", None)
    assert state["fix"]["targets"] == ["c1"]


def test_skipping_sets_a_comment_aside_and_it_can_come_back(convo):
    state = convo["state"]
    loop.skip_comment(state, "c1")
    assert state["comments"][0]["status"] == "skipped" and loop.open_comments(state, "open")[0]["id"] == "c2"
    assert convo["gh"]["replies"] == [] and convo["gh"]["deleted"] == []                    # nothing happens on GitHub
    loop.skip_comment(state, "c2")
    assert state["phase"] == "approved"                                                     # nothing is left for anyone to do
    loop.unskip_comment(state, "c1")
    assert state["comments"][0]["status"] == "open" and state["phase"] == "awaiting_fix"
    with pytest.raises(loop.LoopError):
        loop.unskip_comment(state, "c1")
    state["fix"] = {"before": "x"}
    with pytest.raises(loop.LoopError):
        loop.skip_comment(state, "c1")


def test_failing_tests_block_approval_until_they_pass_or_the_person_overrides(convo, monkeypatch):
    state = convo["state"]
    fake_coder_with_test(monkeypatch, convo["tree"], passing="False")
    loop.run_fix(state, "", None)
    t = state["fix"]["tests"]
    assert t["command"] == "python3 -m unittest -v" and t["report"]["failed"] == 1 and t["report"]["failed_cases"]
    assert "tests fail" in loop.tests_block(state)
    with pytest.raises(loop.LoopError, match="tests fail"):
        loop.approve_fix(state, {}, "")
    assert convo["gh"]["replies"] == [] and ws.git(convo["w"].tree, "rev-parse", "HEAD") == state["fix"]["before"]      # nothing committed or posted
    (convo["tree"] / "test_app.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(True)\n")
    loop.run_tests(state, "python3 -m unittest -v")
    assert state["fix"]["tests"]["report"]["failed"] == 0 and loop.tests_block(state) == "" and state["test_command"]
    loop.approve_fix(state, {}, "")
    assert [c["status"] for c in state["comments"]] == ["fixed", "fixed"]


def test_the_person_can_approve_despite_failures_that_were_not_this_changes(convo, monkeypatch):
    state = convo["state"]
    fake_coder_with_test(monkeypatch, convo["tree"], passing="False")
    loop.run_fix(state, "", None)
    loop.approve_fix(state, {}, "", allow_failing_tests=True)
    assert state["fix"] is None and len(convo["gh"]["replies"]) == 2


def test_no_test_command_or_no_change_does_not_block_but_says_why(convo, monkeypatch):
    state = convo["state"]
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "x"}, {"id": "c2", "outcome": "fixed", "reply": "y"}])
    loop.run_fix(state, "", None)
    assert state["fix"]["tests"]["report"] is None and "No test command" in state["fix"]["tests"]["note"] and loop.tests_block(state) == ""
    with pytest.raises(loop.LoopError):
        loop.run_tests(state, "rm -rf /")                                                    # only a known test runner may be run
    loop.discard_fix(state)
    monkeypatch.setattr(fixer, "run", lambda *a, **k: {"answer": fixer.clean({"summary": "s", "outcomes": [
        {"id": "c1", "outcome": "disagree", "reply": "no"}, {"id": "c2", "outcome": "disagree", "reply": "no"}]}, state["comments"]), "session_id": "s"})
    loop.run_fix(state, "", None)
    assert "changed no file" in state["fix"]["tests"]["note"] and loop.tests_block(state) == ""


# ---------- comments that are already on the pull request ----------

THREADS = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
    {"isResolved": False, "comments": {"nodes": [
        {"databaseId": 21, "body": "This drops the last item.", "path": "app.py", "line": 2, "originalLine": 2, "author": {"login": "alice"}},
        {"databaseId": 22, "body": "Agreed.", "path": "app.py", "line": 2, "originalLine": 2, "author": {"login": "bob"}}]}},
    {"isResolved": True, "comments": {"nodes": [
        {"databaseId": 23, "body": "Already settled.", "path": "app.py", "line": 5, "originalLine": 5, "author": {"login": "alice"}}]}},
    {"isResolved": False, "comments": {"nodes": [
        {"databaseId": 24, "body": "Outdated one.", "path": "app.py", "line": None, "originalLine": 4, "author": None}]}}]}}}}}


def test_unresolved_threads_skip_the_settled_ones_and_keep_replies(monkeypatch):
    monkeypatch.setattr(github, "_request", lambda *a, **k: THREADS)
    got = github.unresolved_threads("o", "r", 7)
    assert [(t["gh_id"], t["line"], t["author"]) for t in got] == [(21, 2, "alice"), (24, 4, "someone")]
    assert got[0]["replies"] == [{"author": "bob", "body": "Agreed.", "gh_id": 22}]


def test_the_coding_agent_can_fix_comments_written_by_people(convo, monkeypatch):
    monkeypatch.setattr(github, "_request", lambda *a, **k: THREADS)
    pr = dict(PR, head_repo="o/r", base_repo="o/r", head_sha=ws.head(convo["w"]))
    state = loop.start_from_github("o", "r", pr, github.unresolved_threads("o", "r", 7))
    assert [(c["id"], c["gh_id"], c["status"]) for c in state["comments"]] == [("c1", 21, "open"), ("c2", 24, "open")]
    assert state["comments"][0]["body"].startswith("**@alice:**") and state["comments"][0]["thread"][0]["text"].startswith("@bob")
    assert state["phase"] == "awaiting_fix"
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "Removed the slice."}])
    loop.run_fix(state, "", None, only=["c1"])
    loop.approve_fix(state, {}, "")
    assert convo["gh"]["replies"] == [(21, loop.CODER_TAG + "Removed the slice.")]            # answered in alice's thread


def test_undoing_the_import_forgets_only_the_local_copy(convo, monkeypatch):
    monkeypatch.setattr(github, "_request", lambda *a, **k: THREADS)
    state = loop.start_from_github("o", "r", dict(PR, head_sha="abc"), github.unresolved_threads("o", "r", 7))
    loop.undo_last(state)
    assert convo["gh"]["deleted"] == [] and loop.load("o", "r", 7) is None                   # other people's comments are never deleted


def test_screen_loads_existing_comments_and_offers_to_fix_them(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import test_review
    test_review.STORE_FOR_TEST = loop.STORE

    def script():
        from review import github, loop, ui
        import test_review as t
        loop.STORE = t.STORE_FOR_TEST
        import review.repos as _rp; _rp.known_repos = lambda: ["o/r"]
        github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="", head_repo="o/r", base_repo="o/r")]
        github._request = lambda *a, **k: t.THREADS
        ui.render()

    at = AppTest.from_function(script, default_timeout=30).run()
    at.text_input[0].set_value("o/r").run()
    assert "📥 Address the review comments already on this pull request" in [b.label for b in at.button]
    at.button(key="rv_load").click().run()
    assert not at.exception, at.exception
    labels = [b.label for b in at.button]
    assert "🛠️ Let the coding agent address all 2 open comment(s)" in labels
    assert labels.count("🛠️ Ask the coding agent") == 2 and labels.count("Skip") == 2 and labels.count("💬 Respond & resolve") == 2


# ---------- comments that lost their link to GitHub ----------

def test_posted_comments_are_matched_by_text_even_when_github_reports_other_lines(convo):
    pr = dict(PR, head_repo="o/r", base_repo="o/r")
    posted = {"url": "u", "comments": [{"path": "app.py", "line": None, "gh_id": 31, "body": "Second."}, {"path": "app.py", "line": None, "gh_id": 30, "body": "First."}]}
    chosen = [{"id": "c1", "path": "app.py", "line": 5, "body": "First."}, {"id": "c2", "path": "app.py", "line": 5, "body": "Second."}]
    state = loop.start("o", "r", pr, posted, chosen)
    assert [(c["id"], c["gh_id"], c["status"]) for c in state["comments"]] == [("c1", 30, "open"), ("c2", 31, "open")]


def _lost(convo):
    for c in convo["state"]["comments"]:
        c["gh_id"], c["status"] = None, "untracked"
    loop._settle(convo["state"])
    return convo["state"]


def test_untracked_comments_are_reconnected_to_their_threads(convo, monkeypatch):
    state = _lost(convo)
    assert state["phase"] == "unlinked"                                                      # not "approved": nothing was resolved, it is stuck
    threads = [{"gh_id": 71, "path": "app.py", "line": 2, "author": "me", "body": "Drops the last item.", "replies": []},
               {"gh_id": 72, "path": "app.py", "line": 5, "author": "me", "body": "Divides by zero on empty input.", "replies": []},
               {"gh_id": 73, "path": "other.py", "line": 1, "author": "x", "body": "unrelated", "replies": []}]
    assert loop.relink(state, threads) == 2
    assert [(c["id"], c["gh_id"], c["status"]) for c in state["comments"]] == [("c1", 71, "open"), ("c2", 72, "open")]
    assert state["phase"] == "awaiting_fix" and loop.relink(state, threads) == 0


def test_the_screen_reconnects_a_stuck_conversation_by_itself(convo, monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    _lost(convo)
    loop.save(convo["state"])
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import test_review
    test_review.STORE_FOR_TEST = loop.STORE
    test_review.RELINK = [{"gh_id": 71, "path": "app.py", "line": 2, "author": "me", "body": "Drops the last item.", "replies": []},
                          {"gh_id": 72, "path": "app.py", "line": 5, "author": "me", "body": "Divides by zero on empty input.", "replies": []}]

    def script():
        from review import github, loop, ui
        import test_review as t
        loop.STORE = t.STORE_FOR_TEST
        import review.repos as _rp; _rp.known_repos = lambda: ["o/r"]
        github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="")]
        github.unresolved_threads = lambda o, n, num: t.RELINK
        ui.render()

    at = AppTest.from_function(script, default_timeout=30).run()
    at.text_input[0].set_value("o/r").run()
    assert not at.exception, at.exception
    labels = [b.label for b in at.button]
    assert "🛠️ Let the coding agent address all 2 open comment(s)" in labels and labels.count("🛠️ Ask the coding agent") == 2


def test_starting_over_shows_a_clean_screen_not_the_old_comments(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import test_review
    test_review.STORE_FOR_TEST = loop.STORE

    def script():
        import llm
        from review import github, loop, ui
        import test_review as t
        loop.STORE = t.STORE_FOR_TEST
        import review.repos as _rp; _rp.known_repos = lambda: ["o/r"]
        github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="", head_repo="o/r", base_repo="o/r")]
        github.get_diff = lambda o, n, num: t.DIFF
        github.get_pr = lambda o, n, num: dict(t.PR)
        github.my_login = lambda: "me"
        github.unresolved_threads = lambda o, n, num: []
        github.delete_comment = lambda o, n, cid: None
        github.post_review = lambda o, n, num, sha, body, ev, cs: {"url": "u", "comments": [{"path": c["path"], "line": c["line"], "gh_id": 1, "body": c["body"]} for c in cs]}
        llm.call_llm_json = lambda *a, **k: {"summary": "OLD SUMMARY", "comments": [
            {"path": "app.py", "line": 5, "severity": "blocker", "comment": "OLD COMMENT", "suggestion_code": ""}]}
        ui.render()

    at = AppTest.from_function(script, default_timeout=30).run()
    at.text_input[0].set_value("o/r").run()
    at.button(key="rv_go").click().run()
    assert any("OLD COMMENT" in t.value for t in at.text_area), "the draft review is shown before posting"
    at.button(key=next(b.key for b in at.button if b.key and b.key.endswith("post"))).click().run()
    assert not at.exception, at.exception
    at.button(key=next(b.key for b in at.button if b.key and b.key.endswith("reset"))).click().run()          # Start over
    assert not at.exception, at.exception
    assert loop.load("o", "r", 7) is None
    assert not at.text_area, "no old comment or summary may come back"
    assert not any("OLD" in m.value for m in at.markdown) and "🔍 Review this pull request" in [b.label for b in at.button]


# ---------- finding repositories by itself ----------

class _Job:
    def __init__(self, source, pr_url=None):
        self.d = {"ws": {"source": source}, "pr": {"url": pr_url} if pr_url else None}


def test_repositories_are_found_from_coding_jobs_reviews_and_settings(monkeypatch, tmp_path):
    from coding import flow
    from review import repos
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setattr(flow, "list_jobs", lambda task_key=None: [                 # newest first, as the real one returns them
        _Job("https://github.com/MythikAI/D2CUser_debesh.git", "https://github.com/MythikAI/D2CUser_debesh/compare/dev...feat?expand=1"),
        _Job("/Users/me/a-local-folder"),                                          # not a GitHub address: ignored
        _Job("https://github.com/MythikAI/D2CUser")])
    loop.STORE.mkdir(parents=True)
    (loop.STORE / "acme__shop__4.json").write_text('{"repo": ["acme", "shop"]}')
    monkeypatch.setenv("REVIEW_GITHUB_REPO", "env/one")
    repos.remember("https://github.com/typed/by-hand.git")
    repos.remember("TYPED/by-hand")                                                 # the same repository again: not duplicated
    repos.remember("not a repo")
    got = repos.known_repos()
    assert got[:2] == ["MythikAI/D2CUser_debesh", "MythikAI/D2CUser"]              # the coding agent's repositories come first
    assert set(got) == {"MythikAI/D2CUser_debesh", "MythikAI/D2CUser", "acme/shop", "typed/by-hand", "env/one"}
    assert len(got) == len({g.lower() for g in got})


def test_screen_lists_pending_prs_of_every_known_repo_with_nothing_typed(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import test_review
    test_review.STORE_FOR_TEST = loop.STORE
    loop.start("acme", "shop", dict(PR, number=8, head_sha="s"), {"url": "u", "comments": []}, [])      # PR #8 already has a conversation

    def script():
        from review import github, loop, repos, ui
        import test_review as t
        loop.STORE = t.STORE_FOR_TEST
        repos.known_repos = lambda: ["acme/shop", "acme/private-no-access", "acme/other"]

        def fake(owner, name):
            if name == "private-no-access":
                raise github.GithubError("GitHub answered 404. Not found", 404)
            return [dict(t.PR, number=8 if name == "shop" else 9, title=f"PR in {name}", url="u", draft=False, updated="")]
        github.list_open_prs = fake
        ui.render()

    at = AppTest.from_function(script, default_timeout=30).run()
    assert not at.exception, at.exception
    options = at.selectbox[0].options
    assert options == ["💬 acme/shop · #8  PR in shop  —  bot  (feat → main)", "acme/other · #9  PR in other  —  bot  (feat → main)"]
    assert any("acme/private-no-access" in w.value for w in at.warning)                  # one unreachable repository does not hide the rest
    assert not any("Enter the repository" in i.value for i in at.info)                   # nothing had to be typed


# ---------- a new review must not pile up on an earlier one ----------

OLD = [  # what GitHub holds from an earlier run of this app, from a person, and from the app's own coding-agent replies
    {"gh_id": 101, "path": "app.py", "line": 2, "author": "Me", "body": "**🔴 Blocker**: Drops the last item.",
     "replies": [{"author": "me", "body": "🛠️ **Coding agent:** fixed", "gh_id": 111}, {"author": "alice", "body": "thanks!", "gh_id": 112}]},
    {"gh_id": 102, "path": "app.py", "line": 5, "author": "me", "body": "**🟡 Suggestion**: Guard the empty case.", "replies": []},
    {"gh_id": 103, "path": "app.py", "line": 5, "author": "alice", "body": "**🔴 Blocker**: a person's own comment", "replies": []},
    {"gh_id": 104, "path": "app.py", "line": 5, "author": "me", "body": "I wrote this by hand, no severity tag", "replies": []}]


def test_earlier_comments_are_found_on_github_even_without_a_record(monkeypatch):
    monkeypatch.setattr(github, "my_login", lambda: "me")
    monkeypatch.setattr(github, "unresolved_threads", lambda *a: OLD)
    got = loop.earlier_app_comments("o", "r", 7)
    assert [(g["root"], g["replies"]) for g in got] == [(101, [111]), (102, [])]            # only the app's own tagged comments; alice's reply stays


def test_clearing_deletes_the_apps_replies_then_its_comments_and_never_other_peoples(monkeypatch):
    deleted = []
    monkeypatch.setattr(github, "delete_comment", lambda o, n, cid: deleted.append(cid))
    monkeypatch.setattr(github, "my_login", lambda: "me")
    monkeypatch.setattr(github, "unresolved_threads", lambda *a: OLD)
    assert loop.clear_comments("o", "r", loop.earlier_app_comments("o", "r", 7)) == 3
    assert deleted == [111, 101, 102] and 103 not in deleted and 112 not in deleted and 104 not in deleted


def test_start_over_deletes_what_the_app_posted_and_keeps_the_state_if_a_delete_fails(convo, monkeypatch):
    state, gh = convo["state"], convo["gh"]
    monkeypatch.setattr(github, "my_login", lambda: "me")
    monkeypatch.setattr(github, "unresolved_threads", lambda *a: OLD[:2])
    monkeypatch.setattr(github, "delete_comment", lambda o, n, cid: (_ for _ in ()).throw(github.GithubError("boom", 500)))
    with pytest.raises(loop.LoopError):
        loop.start_over(state, True)
    assert loop.load("o", "r", 7) is not None                                                  # nothing forgotten: it can be retried
    monkeypatch.setattr(github, "delete_comment", lambda o, n, cid: gh["deleted"].append(cid))
    monkeypatch.setattr(github, "my_review_summaries", lambda *a: [])
    assert loop.start_over(state, True)["deleted"] == 5                                        # 111, 101, 102 + the record's own 11 and 12
    assert sorted(gh["deleted"]) == [11, 12, 101, 102, 111] and loop.load("o", "r", 7) is None


def test_start_over_can_leave_github_alone(convo, monkeypatch):
    monkeypatch.setattr(github, "my_login", lambda: pytest.fail("must not ask GitHub"))
    assert loop.start_over(convo["state"], False) == {"deleted": 0, "notes": []}
    assert convo["gh"]["deleted"] == [] and loop.load("o", "r", 7) is None


def test_a_new_review_warns_about_earlier_comments_and_removes_them_before_posting(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import test_review
    test_review.STORE_FOR_TEST = loop.STORE
    test_review.LOG = []

    def script():
        import llm
        from review import github, loop, repos, ui
        import test_review as t
        loop.STORE = t.STORE_FOR_TEST
        repos.known_repos = lambda: ["o/r"]
        github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="", head_repo="o/r", base_repo="o/r")]
        github.get_diff = lambda o, n, num: t.DIFF
        github.get_pr = lambda o, n, num: dict(t.PR)
        github.my_login = lambda: "me"
        github.unresolved_threads = lambda o, n, num: t.OLD
        github.delete_comment = lambda o, n, cid: t.LOG.append(("delete", cid))
        github.post_review = lambda o, n, num, sha, body, ev, cs: (t.LOG.append(("post", len(cs))), {"url": "u", "comments": []})[1]
        llm.call_llm_json = lambda *a, **k: {"summary": "NEW", "comments": [
            {"path": "app.py", "line": 5, "severity": "nit", "comment": "NEW COMMENT", "suggestion_code": ""}]}
        ui.render()

    at = AppTest.from_function(script, default_timeout=30).run()
    at.button(key="rv_go").click().run()
    assert not at.exception, at.exception
    assert any("2 comment(s) from an earlier review" in w.value for w in at.warning)
    drop = next(c for c in at.checkbox if "earlier comment" in c.label)
    assert drop.value is True                                                                   # on by default: duplicates are the likely problem
    at.button(key=next(b.key for b in at.button if b.key and b.key.endswith("post"))).click().run()
    assert not at.exception, at.exception
    assert test_review.LOG == [("delete", 111), ("delete", 101), ("delete", 102), ("post", 1)]  # cleared first, then posted

    test_review.LOG.clear()                                                                     # again, but this time the person keeps the old ones
    loop.discard(loop.load("o", "r", 7))
    at2 = AppTest.from_function(script, default_timeout=30).run()
    at2.button(key="rv_go").click().run()
    next(c for c in at2.checkbox if "earlier comment" in c.label).uncheck().run()
    at2.button(key=next(b.key for b in at2.button if b.key and b.key.endswith("post"))).click().run()
    assert test_review.LOG == [("post", 1)]                                                     # nothing deleted when the person said keep them


def test_the_reviewer_is_told_what_others_already_said_but_not_the_apps_own_earlier_comments(monkeypatch):
    monkeypatch.setattr(github, "my_login", lambda: "me")
    monkeypatch.setattr(github, "unresolved_threads", lambda *a: OLD)
    mine, others = loop.split_open_threads("o", "r", 7)
    assert [m["root"] for m in mine] == [101, 102] and [o["gh_id"] for o in others] == [103, 104]
    seen = fake_llm(monkeypatch, [])
    reviewer.review(PR, DIFF, existing=others)
    assert "Do NOT repeat" in seen["prompt"] and "a person's own comment" in seen["prompt"]
    assert "Drops the last item" not in seen["prompt"]                                  # the old ones may be deleted: they must be re-raised if still valid
    seen.clear()
    reviewer.review(PR, DIFF)
    assert "already open" not in seen["prompt"]


# ---------- the review's summary comment ----------

def test_posted_summaries_carry_a_hidden_marker_and_the_review_id(monkeypatch):
    sent = []

    def fake(method, path, body=None, accept=""):
        sent.append(body)
        return {"id": 5, "node_id": "PRR_5", "html_url": "u"}
    monkeypatch.setattr(github, "_request", fake)
    out = github.post_review("o", "r", 1, "s", "All good.", "COMMENT", [])
    assert sent[0]["body"] == "All good.\n\n" + github.MARK and out["review_id"] == 5 and out["review_node"] == "PRR_5"
    github.post_review("o", "r", 1, "s", "  ", "COMMENT", [])
    assert sent[1]["body"] == ""                                                               # no summary: nothing to mark or to delete later


REVIEWS = [{"id": 1, "node_id": "N1", "state": "COMMENTED", "html_url": "u1", "user": {"login": "Me"}, "body": "Mine, by the app.\n\n" + github.MARK},
           {"id": 2, "node_id": "N2", "state": "COMMENTED", "html_url": "u2", "user": {"login": "me"}, "body": "Old one, before markers existed."},
           {"id": 3, "node_id": "N3", "state": "APPROVED", "html_url": "u3", "user": {"login": "alice"}, "body": "LGTM"},
           {"id": 4, "node_id": "N4", "state": "COMMENTED", "html_url": "u4", "user": {"login": "me"}, "body": ""}]


def test_my_summaries_are_listed_with_the_ones_the_app_wrote_marked(monkeypatch):
    monkeypatch.setattr(github, "my_login", lambda: "me")
    monkeypatch.setattr(github, "_request", lambda *a, **k: REVIEWS)
    got = github.my_review_summaries("o", "r", 7)
    assert [(g["id"], g["ours"], g["body"]) for g in got] == [(1, True, "Mine, by the app."), (2, False, "Old one, before markers existed.")]


@pytest.mark.parametrize("failing,expected", [(set(), "deleted"), ({"deletePullRequestReview"}, "hidden"),
                                              ({"deletePullRequestReview", "minimizeComment"}, "replaced")])
def test_a_summary_is_removed_the_strongest_way_github_allows(monkeypatch, failing, expected):
    def fake(method, path, body=None, accept=""):
        if method == "POST" and any(f in body["query"] for f in failing):
            return {"errors": [{"message": "no"}]}
        return {"data": {}}
    monkeypatch.setattr(github, "_request", fake)
    assert github.remove_review_summary("o", "r", 7, 1, "N1") == expected


def test_a_summary_github_refuses_to_touch_is_reported_with_a_link(monkeypatch):
    def fake(*a, **k): raise github.GithubError("nope", 422)
    monkeypatch.setattr(github, "_request", fake)
    with pytest.raises(github.GithubError, match="github.com/o/r/pull/7"):
        github.remove_review_summary("o", "r", 7, 1, "N1")
    assert "github.com" in loop.remove_summaries("o", "r", 7, [{"id": 1, "node": "N1"}])[0]        # reported, never raised


def test_start_over_also_removes_the_apps_summaries_but_not_a_persons_own(convo, monkeypatch):
    state = convo["state"]
    state["actions"][0]["undo"]["review"] = {"id": 1, "node": "N1"}                            # the record knows review 1
    removed = []
    monkeypatch.setattr(github, "my_login", lambda: "me")
    monkeypatch.setattr(github, "unresolved_threads", lambda *a: [])
    monkeypatch.setattr(github, "_request", lambda *a, **k: REVIEWS)                          # reviews 1 (ours) and 2 (unmarked, may be a person's)
    monkeypatch.setattr(github, "remove_review_summary", lambda o, n, num, rid, node: removed.append(rid) or "deleted")
    out = loop.start_over(state, True)
    assert removed == [1] and out["notes"] == ["A review summary was deleted."]                # review 2 is left for the person to choose
    assert loop.load("o", "r", 7) is None


def test_start_over_still_finishes_when_github_refuses_a_summary(convo, monkeypatch):
    state = convo["state"]
    state["actions"][0]["undo"]["review"] = {"id": 1, "node": "N1"}
    monkeypatch.setattr(github, "my_login", lambda: "me")
    monkeypatch.setattr(github, "unresolved_threads", lambda *a: [])
    monkeypatch.setattr(github, "_request", lambda *a, **k: REVIEWS)
    monkeypatch.setattr(github, "remove_review_summary", lambda *a: (_ for _ in ()).throw(github.GithubError("GitHub would not remove this summary.", 422)))
    out = loop.start_over(state, True)
    assert "would not remove" in out["notes"][0] and loop.load("o", "r", 7) is None


def test_undoing_the_review_removes_its_summary_too(convo, monkeypatch):
    state = convo["state"]
    state["actions"][0]["undo"]["review"] = {"id": 9, "node": "N9"}
    removed = []
    monkeypatch.setattr(github, "remove_review_summary", lambda o, n, num, rid, node: removed.append(rid) or "hidden")
    out = loop.undo_last(state)
    assert removed == [9] and out["undo_notes"] == ["A review summary was hidden (collapsed as outdated)."]


def test_screen_lists_old_summaries_and_removes_only_the_ones_left_ticked(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import test_review
    test_review.STORE_FOR_TEST = loop.STORE
    test_review.LOG = []

    def script():
        from review import github, loop, repos, ui
        import test_review as t
        loop.STORE = t.STORE_FOR_TEST
        repos.known_repos = lambda: ["o/r"]
        github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="", head_repo="o/r", base_repo="o/r")]
        github.my_login = lambda: "me"
        github.unresolved_threads = lambda o, n, num: []
        github._request = lambda *a, **k: t.REVIEWS
        github.remove_review_summary = lambda o, n, num, rid, node: (t.LOG.append(rid), "deleted")[1]
        ui.render()

    at = AppTest.from_function(script, default_timeout=30).run()
    assert not at.exception, at.exception
    at.button(key=next(b.key for b in at.button if b.key and b.key.endswith("_look"))).click().run()
    boxes = {c.key: c.value for c in at.checkbox if c.key and "summ_" in c.key}
    assert list(boxes.values()) == [True, False], "only the one this app wrote is ticked for removal"
    assert any("Old one, before markers existed" in m.value for m in at.markdown)
    at.button(key=next(b.key for b in at.button if b.key and b.key.endswith("_go"))).click().run()
    assert test_review.LOG == [1] and not at.exception
    assert any("deleted" in i.value for i in at.info)


def test_a_fresh_review_offers_every_old_summary_deletes_the_ticked_ones_before_posting(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import test_review
    test_review.STORE_FOR_TEST = loop.STORE
    test_review.LOG = []

    def script():
        import llm
        from review import github, loop, repos, ui
        import test_review as t
        loop.STORE = t.STORE_FOR_TEST
        repos.known_repos = lambda: ["o/r"]
        github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="", head_repo="o/r", base_repo="o/r")]
        github.get_diff = lambda o, n, num: t.DIFF
        github.get_pr = lambda o, n, num: dict(t.PR)
        github.my_login = lambda: "me"
        github.unresolved_threads = lambda o, n, num: []
        github._request = lambda *a, **k: t.REVIEWS                                        # reviews 1 (marked) and 2 (older, unmarked) are mine
        github.remove_review_summary = lambda o, n, num, rid, node: (t.LOG.append(("summary", rid)), "deleted")[1]
        github.post_review = lambda o, n, num, sha, body, ev, cs: (t.LOG.append(("post", len(cs))), {"url": "u", "comments": []})[1]
        llm.call_llm_json = lambda *a, **k: {"summary": "NEW", "comments": [
            {"path": "app.py", "line": 5, "severity": "nit", "comment": "NEW COMMENT", "suggestion_code": ""}]}
        ui.render()

    at = AppTest.from_function(script, default_timeout=30).run()
    at.button(key="rv_go").click().run()
    assert not at.exception, at.exception
    assert any("2 summary comment(s) from earlier reviews" in w.value for w in at.warning)
    boxes = [c for c in at.checkbox if c.key and "_sum_" in c.key]
    assert [c.value for c in boxes] == [True, True]                                      # the old, unmarked one is offered too, ticked
    boxes[1].uncheck().run()                                                              # the person keeps review 2
    at.button(key=next(b.key for b in at.button if b.key and b.key.endswith("post"))).click().run()
    assert not at.exception, at.exception
    assert test_review.LOG == [("summary", 1), ("post", 1)]                               # the ticked one first, then the new review
    assert any("deleted" in i.value for i in at.info)


# ---------- the reviewer with the repository (the way Claude Code reviews) ----------

from review import checkout


def _pr_remote(tmp_path):
    """A 'GitHub': a bare remote whose pull/7/head points at a commit that is on no branch (as for a fork's PR)."""
    remote, work = tmp_path / "gh.git", tmp_path / "w"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    sh(tmp_path, "clone", "-q", str(remote), str(work))
    (work / "app.py").write_text("v1\n"); sh(work, "add", "-A"); sh(work, "commit", "-q", "-m", "one"); sh(work, "push", "-q", "origin", "HEAD:main")
    sh(work, "checkout", "-q", "-b", "fork-branch")
    (work / "app.py").write_text("pr code\n"); sh(work, "commit", "-qam", "pr"); sha = ws.git(work, "rev-parse", "HEAD")
    sh(work, "push", "-q", str(remote), f"{sha}:refs/pull/7/head")
    return remote, work, sha


def test_checkout_fetches_the_prs_own_commit_resets_it_and_follows_updates(tmp_path, monkeypatch):
    from review import checkout as co
    monkeypatch.setattr(co, "ROOT", tmp_path / "rv")
    monkeypatch.setattr(co, "prepare", REAL_PREPARE)                                             # the autouse "offline" stub off for this test
    remote, work, sha = _pr_remote(tmp_path)
    tree = co.prepare("o", "r", 7, remote=str(remote))
    assert ws.git(tree, "rev-parse", "HEAD") == sha and (tree / "app.py").read_text() == "pr code\n"      # a fork's commit, on no branch
    (tree / "app.py").write_text("scribbled"); (tree / "junk.txt").write_text("x")
    (work / "app.py").write_text("pr code v2\n"); sh(work, "commit", "-qam", "pr2"); sha2 = ws.git(work, "rev-parse", "HEAD")
    sh(work, "push", "-q", "-f", str(remote), f"{sha2}:refs/pull/7/head")                         # the author pushed again
    tree2 = co.prepare("o", "r", 7, remote=str(remote))
    assert tree2 == tree and ws.git(tree, "rev-parse", "HEAD") == sha2
    assert (tree / "app.py").read_text() == "pr code v2\n" and not (tree / "junk.txt").exists()       # clean every time
    with pytest.raises(co.CheckoutError):
        co.prepare("o", "r", 99, remote=str(remote))                                              # no such pull request


def test_the_github_token_is_given_to_git_only_through_its_environment(monkeypatch):
    from review import checkout as co
    monkeypatch.setenv("GITHUB_TOKEN", "sekret")
    env = co._env("https://github.com/o/r.git")
    assert "sekret" not in env["GIT_CONFIG_VALUE_0"] and env["GIT_CONFIG_COUNT"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" not in co._env("/some/local/path") or os.environ.get("GIT_CONFIG_COUNT")


import os


class FakeAgent:
    """Stands in for coding_agent._call: records how it was called, answers explore and verify turns from a script."""

    def __init__(self, monkeypatch, explore, verify=None):
        self.calls = []
        self.explore, self.verify = explore, verify

        def fake(label, agent, prompt, system, cwd, model, resume, on_event, schema, tools, disallowed, max_turns=80):
            self.calls.append({"label": label, "prompt": prompt, "system": system, "cwd": cwd, "tools": tools, "disallowed": disallowed,
                               "turns": max_turns})
            if on_event:
                on_event(f"{label} step")
            return {"answer": {"review": self.explore, "review-verify": self.verify, "review-recheck": self.explore}[label], "session_id": "s"}
        monkeypatch.setattr(coding_agent, "_call", fake)


from coding import agent as coding_agent


def test_the_reviewer_explores_with_real_tools_then_a_second_look_checks_each_finding(monkeypatch, tmp_path):
    explore = {"summary": "Merge after fixes.", "comments": [
        {"path": "app.py", "line": 5, "severity": "blocker", "comment": "Skips the last item.", "suggestion_code": "", "evidence": "Read app.py and traced total()."},
        {"path": "app.py", "line": 8, "severity": "nit", "comment": "Rename it.", "suggestion_code": "", "evidence": "style"},
        {"path": "app.py", "line": 6, "severity": "suggestion", "comment": "Unused variable.", "suggestion_code": "", "evidence": "none"},
        {"path": "gone.py", "line": 1, "severity": "nit", "comment": "Outside the diff.", "suggestion_code": "", "evidence": "x"}]}
    verify = {"verdicts": [{"id": "c1", "confirmed": True, "reason": "Grepped callers: avg() passes the list unchanged.", "severity": "blocker",
                            "comment": "Off by one: the loop stops before the last item, so avg() is wrong."},
                           {"id": "c2", "confirmed": False, "reason": "Opened the file: the name matches the module's convention."}]}   # c3 not ruled on
    fake = FakeAgent(monkeypatch, explore, verify)
    monkeypatch.setattr(checkout, "prepare", lambda o, n, num: tmp_path)
    events = []
    out = reviewer.review_pr("o", "r", PR, DIFF, existing=[{"path": "x.py", "line": 1, "author": "bob", "body": "already said"}], on_event=events.append)
    assert [c["label"] for c in fake.calls] == ["review", "review-verify"] and out["mode"] == "repo"
    for call in fake.calls:
        assert call["cwd"] == str(tmp_path) and set(call["tools"]) == {"Read", "Glob", "Grep", "Bash"}      # reads, searches, runs; never writes
        assert "Bash(git push:*)" in call["disallowed"] and "Bash(curl:*)" in call["disallowed"] and "Bash(git fetch:*)" in call["disallowed"]
    assert "already said" in fake.calls[0]["prompt"] and "evidence" in fake.calls[0]["system"] and "second reviewer" in fake.calls[1]["system"]
    kept = {c["id"]: c for c in out["comments"]}
    assert set(kept) == {"c1", "c3"} and kept["c1"]["verified"] is True and kept["c3"]["verified"] is False   # c4 outside diff dropped, c2 rejected
    assert "Off by one" in kept["c1"]["body"] and kept["c1"]["evidence"].startswith("Grepped callers")                # reworded by the second look
    assert [(r["line"], r["reason"][:12]) for r in out["rejected"]] == [(8, "Opened the f")]
    assert any("discarded" in n for n in out["notes"]) and any("second look" in n for n in out["notes"]) and any("unchecked" in n for n in out["notes"])
    assert "reviewer is reading" in " ".join(events) and "checking 3 finding" in " ".join(events)


def test_no_findings_means_no_second_look(monkeypatch, tmp_path):
    fake = FakeAgent(monkeypatch, {"summary": "Looks sound.", "comments": []})
    monkeypatch.setattr(checkout, "prepare", lambda o, n, num: tmp_path)
    out = reviewer.review_pr("o", "r", PR, DIFF)
    assert [c["label"] for c in fake.calls] == ["review"] and out["comments"] == [] and out["rejected"] == []


def test_when_the_code_cannot_be_fetched_it_falls_back_to_the_diff_and_says_so(monkeypatch):
    fake_llm(monkeypatch, [{"path": "app.py", "line": 5, "severity": "nit", "comment": "x", "suggestion_code": ""}])
    out = reviewer.review_pr("o", "r", PR, DIFF)                                                 # the autouse stub makes the checkout fail
    assert out["mode"] == "diff" and "could NOT check out" in out["notes"][0] and "offline in tests" in out["notes"][0]
    assert [c["id"] for c in out["comments"]] == ["c1"] and out["rejected"] == []


def test_the_recheck_reads_the_code_and_verifies_new_findings(monkeypatch, tmp_path):
    answer = {"summary": "Settled.", "verdicts": [{"id": "c1", "resolved": True, "reply": "Fixed; I read avg() and ran its test."}],
              "comments": [{"path": "app.py", "line": 6, "severity": "nit", "comment": "New thing.", "suggestion_code": "", "evidence": "read it"}]}
    fake = FakeAgent(monkeypatch, answer, {"verdicts": [{"id": "c2", "confirmed": False, "reason": "Not a problem."}]})
    out = reviewer.recheck(PR, [{"id": "c1", "path": "app.py", "line": 5, "body": "b", "thread": []}], DIFF, DIFF, 2, tree=str(tmp_path))
    assert [c["label"] for c in fake.calls] == ["review-recheck", "review-verify"] and out["mode"] == "repo"
    assert "Do not resolve a thread on the strength of the reply" in fake.calls[0]["system"]
    assert out["verdicts"][0]["resolved"] is True and out["comments"] == [] and len(out["rejected"]) == 1


def test_screen_shows_evidence_and_what_the_second_look_discarded(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import test_review
    test_review.STORE_FOR_TEST = loop.STORE
    test_review.TREE = tmp_path

    def script():
        from review import checkout, github, loop, repos, ui
        import test_review as t
        loop.STORE = t.STORE_FOR_TEST
        repos.known_repos = lambda: ["o/r"]
        github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="", head_repo="o/r", base_repo="o/r")]
        github.get_diff = lambda o, n, num: t.DIFF
        github.my_login = lambda: "me"
        github.unresolved_threads = lambda o, n, num: []
        github.my_review_summaries = lambda o, n, num: []
        checkout.prepare = lambda o, n, num: t.TREE
        from coding import agent
        answers = {"review": {"summary": "S", "comments": [
            {"path": "app.py", "line": 5, "severity": "blocker", "comment": "Real bug.", "suggestion_code": "", "evidence": "read it"},
            {"path": "app.py", "line": 6, "severity": "nit", "comment": "False alarm.", "suggestion_code": "", "evidence": "guess"}]},
            "review-verify": {"verdicts": [{"id": "c1", "confirmed": True, "reason": "Traced total()."},
                                           {"id": "c2", "confirmed": False, "reason": "Handled in the caller."}]}}
        agent._call = lambda label, *a, **k: {"answer": answers[label], "session_id": "s"}
        ui.render()

    at = AppTest.from_function(script, default_timeout=30).run()
    at.button(key="rv_go").click().run()
    assert not at.exception, at.exception
    assert any("Checked against the code: Traced total()" in c.value for c in at.caption)
    assert [t.value for t in at.text_area if "body_" in (t.key or "")] == ["**🔴 Blocker**: Real bug."]            # only the confirmed one is offered
    assert any("discarded (1)" in e.label for e in at.expander)
    assert any("Handled in the caller" in c.value for c in at.caption)


def test_a_fork_pull_request_is_read_but_its_code_is_never_run(monkeypatch, tmp_path):
    fake = FakeAgent(monkeypatch, {"summary": "ok", "comments": [
        {"path": "app.py", "line": 5, "severity": "nit", "comment": "x", "suggestion_code": "", "evidence": "read it"}]},
        {"verdicts": [{"id": "c1", "confirmed": True, "reason": "read it"}]})
    monkeypatch.setattr(checkout, "prepare", lambda o, n, num: tmp_path)
    own = reviewer.review_pr("o", "r", dict(PR, head_repo="o/r", base_repo="o/r"), DIFF)
    assert all("Bash" in c["tools"] for c in fake.calls) and not any("fork" in n for n in own["notes"])
    fake.calls.clear()
    forked = reviewer.review_pr("o", "r", dict(PR, head_repo="stranger/r", base_repo="o/r"), DIFF)
    assert [c["label"] for c in fake.calls] == ["review", "review-verify"]
    assert all(set(c["tools"]) == {"Read", "Glob", "Grep"} for c in fake.calls)                  # no shell for either pass
    assert "NO shell" in fake.calls[0]["prompt"] and any("fork" in n for n in forked["notes"])


# ---------- a person answers and resolves a comment themselves ----------

def test_respond_and_resolve_posts_the_persons_reply_resolves_the_thread_and_can_be_undone(convo):
    state, gh = convo["state"], convo["gh"]
    loop.respond_and_resolve(state, "c1", "  I fixed this by hand in the next commit.  ")
    c1 = state["comments"][0]
    assert gh["replies"] == [(11, loop.HUMAN_TAG + "I fixed this by hand in the next commit.")] and gh["resolved"] == [11]
    assert (c1["status"], c1["by"]) == ("resolved", "human") and c1["thread"] == [{"who": "human", "text": "I fixed this by hand in the next commit.", "gh_id": 501}]
    assert state["comments"][1]["status"] == "open" and state["phase"] == "awaiting_fix"
    assert "Replied and resolved" in loop.last_summary(state)
    with pytest.raises(loop.LoopError, match="no answered comments"):
        loop.run_recheck(state)                                                              # a person's word settles it: the reviewer never re-judges it
    loop.undo_last(state)
    assert gh["reopened"] == [11] and gh["deleted"] == [501]
    assert state["comments"][0]["status"] == "open" and state["comments"][0]["thread"] == [] and "by" not in state["comments"][0]


@pytest.mark.parametrize("status", ["open", "fixed", "disputed", "skipped"])
def test_any_unsettled_comment_can_be_answered_by_a_person(convo, status):
    convo["state"]["comments"][0]["status"] = status
    loop.respond_and_resolve(convo["state"], "c1", "Done.")
    assert convo["state"]["comments"][0]["status"] == "resolved"


def test_respond_and_resolve_refuses_what_it_should(convo):
    state = convo["state"]
    with pytest.raises(loop.LoopError, match="Write a reply"):
        loop.respond_and_resolve(state, "c1", "   ")
    with pytest.raises(loop.LoopError):
        loop.respond_and_resolve(state, "c9", "x")                                           # no such comment
    state["comments"][1]["gh_id"], state["comments"][1]["status"] = None, "untracked"
    with pytest.raises(loop.LoopError):
        loop.respond_and_resolve(state, "c2", "x")                                           # not tied to a thread
    state["fix"] = {"before": "x"}
    with pytest.raises(loop.LoopError, match="waiting for your approval"):
        loop.respond_and_resolve(state, "c1", "x")
    assert convo["gh"]["replies"] == []


def test_if_the_reply_cannot_be_posted_nothing_changes_and_if_only_resolving_fails_it_says_so(convo, monkeypatch):
    state = convo["state"]
    monkeypatch.setattr(github, "reply", lambda *a: (_ for _ in ()).throw(github.GithubError("boom", 500)))
    with pytest.raises(loop.LoopError, match="nothing was changed"):
        loop.respond_and_resolve(state, "c1", "x")
    assert state["comments"][0]["status"] == "open" and not state.get("actions", [])[1:]
    monkeypatch.setattr(github, "reply", lambda *a: 777)
    monkeypatch.setattr(github, "resolve_threads", lambda *a, **k: (_ for _ in ()).throw(github.GithubError("no permission", 403)))
    loop.respond_and_resolve(state, "c1", "x")
    assert state["comments"][0]["status"] == "resolved" and "not marked resolved" in state["notes"][0]      # the reply is there; the person is told


# ---------- send some comments back to the reviewer ----------

def test_the_person_picks_which_comments_the_reviewer_looks_at_again_even_one_they_skipped(convo, monkeypatch):
    state = convo["state"]
    state["comments"][0].update(status="fixed", thread=[{"who": "coder", "text": "done", "gh_id": 1}])
    loop.skip_comment(state, "c2")
    monkeypatch.setattr(github, "get_pr", lambda *a: dict(PR, head_sha="abc"))
    monkeypatch.setattr(github, "compare_diff", lambda *a: DIFF)
    monkeypatch.setattr(github, "get_diff", lambda *a: DIFF)
    seen = {}

    def fake(prompt, *a, **k):
        seen["prompt"] = prompt
        return {"summary": "s", "verdicts": [{"id": "c2", "resolved": False, "reply": "Still not handled."}], "comments": []}
    monkeypatch.setattr(llm, "call_llm_json", fake)
    loop.run_recheck(state, only=["c2"])
    assert "Thread c2" in seen["prompt"] and "Thread c1" not in seen["prompt"]                 # only the one that was picked
    loop.approve_recheck(state, {}, {}, [], "s")
    assert [c["status"] for c in state["comments"]] == ["fixed", "open"]                       # the skipped one is back with the coding agent
    with pytest.raises(loop.LoopError):
        loop.run_recheck(state, only=["c9"])


def test_skipped_comments_are_not_judged_unless_picked(convo):
    loop.skip_comment(convo["state"], "c1")
    with pytest.raises(loop.LoopError, match="no answered comments"):
        loop.run_recheck(convo["state"])


# ---------- merging ----------

def _gh_responses(**over):
    pr = {"state": "open", "merged": False, "draft": False, "mergeable": True, "mergeable_state": "clean", "title": "t", "head": {"sha": "abc"},
          "base": {"ref": "main"}}
    table = {"pulls": {**pr, **over.get("pr", {})}, "repo": over.get("repo", {"allow_merge_commit": True, "allow_squash_merge": True, "allow_rebase_merge": False}),
             "check-runs": over.get("runs", {"check_runs": []}), "status": over.get("status", {"statuses": []})}

    def fake(method, path, body=None, accept=""):
        for k in ("check-runs", "status"):
            if path.endswith(k) or f"/{k}?" in path:
                if isinstance(table[k], Exception):
                    raise table[k]
                return table[k]
        return table["pulls"] if "/pulls/" in path else table["repo"]
    return fake


def test_merge_info_reports_what_github_would_do_and_what_the_checks_say(monkeypatch):
    monkeypatch.setattr(github, "_request", _gh_responses())
    got = github.merge_info("o", "r", 7)
    assert got["mergeable_state"] == "clean" and got["methods"] == ["merge", "squash"] and got["failing"] == got["pending"] == [] and got["checks_known"]
    monkeypatch.setattr(github, "_request", _gh_responses(
        runs={"check_runs": [{"name": "build", "status": "completed", "conclusion": "failure"}, {"name": "lint", "status": "in_progress"},
                             {"name": "ok", "status": "completed", "conclusion": "success"}]},
        status={"statuses": [{"context": "ci/x", "state": "error"}, {"context": "ci/y", "state": "pending"}]}, pr={"draft": True}))
    got = github.merge_info("o", "r", 7)
    assert got["failing"] == ["build", "ci/x"] and got["pending"] == ["lint", "ci/y"] and got["draft"] is True


def test_checks_that_cannot_be_read_are_never_reported_as_passing(monkeypatch):
    monkeypatch.setattr(github, "_request", _gh_responses(runs=github.GithubError("forbidden", 403)))
    assert github.merge_info("o", "r", 7)["checks_known"] is False


def test_merging_goes_through_githubs_own_endpoint_pinned_to_the_commit_that_was_seen(monkeypatch):
    sent = []

    def fake(method, path, body=None, accept=""):
        sent.append((method, path, body))
        return {"merged": True, "sha": "deadbeef", "message": "Pull Request successfully merged"}
    monkeypatch.setattr(github, "_request", fake)
    out = github.merge_pr("o", "r", 7, "abc", "squash")
    assert sent == [("PUT", "/repos/o/r/pulls/7/merge", {"merge_method": "squash", "sha": "abc"})] and out["merged"] and out["sha"] == "deadbeef"
    with pytest.raises(github.GithubError, match="Unsupported"):
        github.merge_pr("o", "r", 7, "abc", "force")


@pytest.mark.parametrize("status,phrase", [(405, "will not merge"), (409, "New commits were pushed")])
def test_a_refused_merge_says_why(monkeypatch, status, phrase):
    monkeypatch.setattr(github, "_request", lambda *a, **k: (_ for _ in ()).throw(github.GithubError("raw", status)))
    with pytest.raises(github.GithubError, match=phrase):
        github.merge_pr("o", "r", 7, "abc", "merge")


def test_after_a_merge_the_conversation_is_finished_and_nothing_can_be_undone(convo):
    state = convo["state"]
    assert [c["id"] for c in loop.unsettled(state)] == ["c1", "c2"]
    loop.mark_merged(state, "deadbeef")
    assert state["phase"] == "merged" and loop.load("o", "r", 7)["merged"]["sha"] == "deadbeef"
    assert "merged" in loop.undo_blocker(state)
    with pytest.raises(loop.LoopError):
        loop.undo_last(state)


def test_the_confirmation_sentences_say_what_was_pushed_and_posted(convo, monkeypatch):
    state = convo["state"]
    fake_coder(monkeypatch, convo["tree"], [{"id": "c1", "outcome": "fixed", "reply": "ok"}, {"id": "c2", "outcome": "disagree", "reply": "no"}])
    loop.run_fix(state, "", None)
    loop.approve_fix(state, {}, "")
    sha = ws.head(convo["w"])[:8]
    assert loop.last_summary(state) == f"✅ Pushed `{sha}` to `feat`; posted 2 replies."


# ---------- the screen: what a person can do once the comments are on GitHub ----------

SCREEN = {}


def _screen_script():
    from review import github, loop, repos, ui
    import test_review as t
    loop.STORE = t.STORE_FOR_TEST
    repos.known_repos = lambda: ["o/r"]
    github.list_open_prs = lambda o, n: [dict(t.PR, url="u", draft=False, updated="", head_repo="o/r", base_repo="o/r", head_sha="abc")]
    github.unresolved_threads = lambda o, n, num: []
    for name, fn in t.SCREEN.items():
        setattr(github, name, fn)
    ui.render()


def _open_screen(monkeypatch, tmp_path, statuses=("open", "open"), **stubs):
    """A saved conversation on PR #7 with two comments in the given statuses, and the screen opened on it."""
    from streamlit.testing.v1 import AppTest
    import test_review
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    test_review.STORE_FOR_TEST = loop.STORE
    SCREEN.clear()
    SCREEN.update(stubs)
    chosen = [{"id": "c1", "path": "app.py", "line": 2, "body": "First."}, {"id": "c2", "path": "app.py", "line": 5, "body": "Second."}]
    posted = {"url": "u", "comments": [{"path": "app.py", "line": 2, "gh_id": 11, "body": "First."}, {"path": "app.py", "line": 5, "gh_id": 12, "body": "Second."}]}
    state = loop.start("o", "r", dict(PR, head_repo="o/r", base_repo="o/r", head_sha="abc"), posted, chosen)
    for c, st_ in zip(state["comments"], statuses):
        c["status"] = st_
    loop._settle(state)
    loop.save(state)
    return state, AppTest.from_function(_screen_script, default_timeout=30).run()


def test_screen_respond_and_resolve_posts_the_reply_and_confirms(monkeypatch, tmp_path):
    log = []
    state, at = _open_screen(monkeypatch, tmp_path, reply=lambda o, n, num, cid, body: (log.append((cid, body)), 900)[1],
                             resolve_threads=lambda o, n, num, ids, resolved=True: (log.append(("resolve", ids)), list(ids))[1])
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "💬 Respond & resolve").click().run()
    box = next(t for t in at.text_area if "rtext_c1" in (t.key or ""))
    assert box.value == "Addressed."                                                         # a sensible default the person can edit
    box.set_value("Not a bug: the caller guarantees a non-empty list.").run()
    next(b for b in at.button if b.label == "Post the reply and resolve").click().run()
    assert not at.exception, at.exception
    assert log == [(11, loop.HUMAN_TAG + "Not a bug: the caller guarantees a non-empty list."), ("resolve", [11])]
    assert any("Replied and resolved" in s.value for s in at.success)
    assert loop.load("o", "r", 7)["comments"][0]["status"] == "resolved"
    assert any("1 resolved · 1 open" in m.value for m in at.markdown)


def test_screen_review_again_lets_the_person_pick_which_comments_to_send_back(monkeypatch, tmp_path):
    state, at = _open_screen(monkeypatch, tmp_path, statuses=("fixed", "skipped"))
    boxes = [c for c in at.checkbox if "again_" in (c.key or "")]
    assert [c.value for c in boxes] == [True, False]                                         # answered: ticked; set aside: not
    assert "🔍 Ask the reviewer to review the 1 selected again" in [b.label for b in at.button]
    boxes[1].check().run()
    assert "🔍 Ask the reviewer to review the 2 selected again" in [b.label for b in at.button]
    boxes[0].uncheck().run(); boxes[1].uncheck().run()
    assert next(b for b in at.button if b.label.startswith("🔍 Ask the reviewer")).disabled is True      # nothing picked: nothing to ask


def test_screen_merge_with_nothing_overridden_needs_only_a_tick(monkeypatch, tmp_path):
    merged = []
    info = {"state": "open", "merged": False, "draft": False, "mergeable": True, "mergeable_state": "clean", "head_sha": "abc", "title": "t",
            "base": "main", "failing": [], "pending": [], "checks_known": True, "methods": ["merge", "squash"]}
    state, at = _open_screen(monkeypatch, tmp_path, statuses=("resolved", "resolved"), merge_info=lambda o, n, num: info,
                             merge_pr=lambda o, n, num, sha, method: (merged.append((sha, method)), {"merged": True, "sha": "deadbeef1234", "message": ""})[1])
    assert any("Every comment is settled" in s.value for s in at.success)
    next(b for b in at.button if b.label == "Check the merge status").click().run()
    assert any("Everything is settled, the checks pass" in s.value for s in at.success) and not at.warning
    go = next(b for b in at.button if b.label == "🔀 Merge now")
    assert go.disabled is True
    next(c for c in at.checkbox if "mok" in (c.key or "")).check().run()
    next(r for r in at.radio if "mmethod" in (r.key or "")).set_value("squash").run()
    next(b for b in at.button if b.label == "🔀 Merge now").click().run()
    assert not at.exception, at.exception
    assert merged == [("abc", "squash")]                                                     # the commit the person looked at, the method they chose
    assert any("Merged pull request #7 (commit `deadbeef`)" in s.value for s in at.success)
    assert any("This pull request was merged" in s.value for s in at.success)               # and the conversation is now a finished one
    assert loop.load("o", "r", 7)["phase"] == "merged"


def test_screen_overriding_comments_and_checks_takes_a_typed_confirmation(monkeypatch, tmp_path):
    merged = []
    info = {"state": "open", "merged": False, "draft": False, "mergeable": True, "mergeable_state": "unstable", "head_sha": "newer", "title": "t",
            "base": "main", "failing": ["build"], "pending": ["lint"], "checks_known": True, "methods": ["merge"]}
    state, at = _open_screen(monkeypatch, tmp_path, statuses=("open", "skipped"), merge_info=lambda o, n, num: info,
                             unresolved_threads=lambda o, n, num: [{"gh_id": 1}, {"gh_id": 2}, {"gh_id": 3}],
                             merge_pr=lambda o, n, num, sha, method: (merged.append(sha), {"merged": True, "sha": "cafe0000", "message": ""})[1])
    next(b for b in at.button if b.label == "Check the merge status").click().run()
    warning = " ".join(w.value for w in at.warning)
    for expected in ("2 comment(s) here are not resolved (1 open, 1 skipped)", "3 unresolved conversation(s) on GitHub", "failing checks: build",
                     "checks still running: lint", "commits were pushed after the last review"):
        assert expected in warning, expected
    go = lambda: next(b for b in at.button if b.label == "🔀 Merge now")
    assert go().disabled is True
    next(t for t in at.text_input if "mtype" in (t.key or "")).set_value("yes please").run()
    assert go().disabled is True                                                             # only the word "merge" unlocks it
    next(t for t in at.text_input if "mtype" in (t.key or "")).set_value("Merge").run()
    go().click().run()
    assert merged == ["newer"] and any("overriding 5 warning(s)" in s.value for s in at.success)


def test_screen_shows_githubs_refusal_and_merges_nothing(monkeypatch, tmp_path):
    info = {"state": "open", "merged": False, "draft": False, "mergeable": True, "mergeable_state": "clean", "head_sha": "abc", "title": "t",
            "base": "main", "failing": [], "pending": [], "checks_known": True, "methods": ["merge"]}

    def refuse(o, n, num, sha, method):
        raise github.GithubError("GitHub will not merge this pull request now (a required review is missing).", 405)
    state, at = _open_screen(monkeypatch, tmp_path, statuses=("resolved", "resolved"), merge_info=lambda o, n, num: info, merge_pr=refuse)
    next(b for b in at.button if b.label == "Check the merge status").click().run()
    next(c for c in at.checkbox if "mok" in (c.key or "")).check().run()
    next(b for b in at.button if b.label == "🔀 Merge now").click().run()
    assert any("required review is missing" in e.value for e in at.error)
    assert loop.load("o", "r", 7).get("merged") is None


@pytest.mark.parametrize("patch,phrase", [({"merged": True}, "already merged"), ({"state": "closed"}, "already closed"), ({"draft": True}, "draft")])
def test_screen_does_not_offer_to_merge_what_cannot_be_merged(monkeypatch, tmp_path, patch, phrase):
    info = {"state": "open", "merged": False, "draft": False, "mergeable": True, "mergeable_state": "clean", "head_sha": "abc", "title": "t",
            "base": "main", "failing": [], "pending": [], "checks_known": True, "methods": ["merge"], **patch}
    state, at = _open_screen(monkeypatch, tmp_path, merge_info=lambda o, n, num: info)
    next(b for b in at.button if b.label == "Check the merge status").click().run()
    assert any(phrase in (m.value) for m in list(at.info) + list(at.warning))
    assert not [b for b in at.button if b.label == "🔀 Merge now"]


def test_screen_after_a_merge_shows_a_finished_conversation(monkeypatch, tmp_path):
    state, at = _open_screen(monkeypatch, tmp_path)
    loop.mark_merged(loop.load("o", "r", 7), "deadbeef00")
    at.run()
    assert any("This pull request was merged (commit `deadbeef`)" in s.value for s in at.success)
    assert not [b for b in at.button if "Ask the coding agent" in b.label or b.label == "🔀 Merge now"]
    assert all(b.disabled for b in at.button if b.label.startswith("↶ Undo"))                 # nothing can be undone once merged


def test_start_over_when_github_refuses_shows_why_and_offers_to_start_over_without_touching_github(monkeypatch, tmp_path):
    state, at = _open_screen(monkeypatch, tmp_path, statuses=("resolved", "resolved"), my_login=lambda: "me",
                             delete_comment=lambda o, n, cid: (_ for _ in ()).throw(github.GithubError("GitHub answered 403", 403)),
                             my_review_summaries=lambda *a: [])
    next(b for b in at.button if b.key.endswith("reset")).click().run()
    assert not at.exception, at.exception
    assert any("nothing was forgotten" in e.value and "403" in e.value for e in at.error)         # the reason is on screen, not silence
    assert loop.load("o", "r", 7) is not None and any(b.label == "Start over without touching GitHub" for b in at.button)
    assert any("All comments" in e.label for e in at.expander)                                    # and the rest of the screen is still there
    next(b for b in at.button if b.label == "Start over without touching GitHub").click().run()
    assert loop.load("o", "r", 7) is None and any("left as they are" in i.value for i in at.info)


# ---------- which agents the page shows, and when ----------

def test_the_later_agents_are_unlocked_one_step_at_a_time(monkeypatch, tmp_path):
    import json
    import progress
    from coding import flow as coding_flow

    class Job:
        def __init__(self, phase="planning", pr=None, push=None, merged=False, branch="feat", run="r1"):
            from coding import workspace as ws_
            self.phase = phase
            self.d = {"task": {"run_id": run}, "pr": pr, "push": push, "merged": merged, "ws": {"source": "https://github.com/o/r.git"}}
            self.workspace = ws_.Workspace(None, None, branch, "dev", "x", "https://github.com/o/r.git")
    jobs = []
    monkeypatch.setattr(coding_flow, "list_jobs", lambda task_key=None: jobs)
    monkeypatch.setattr(loop, "STORE", tmp_path / "store")
    assert progress.flags("", False) == {"em2": False, "review": False, "qa": False}                  # a fresh page: nothing yet
    assert progress.flags("r1", True) == {"em2": True, "review": False, "qa": False}                  # a release plan: Engineering Manager 2
    jobs.append(Job(phase="executing"))
    assert progress.flags("r1", True)["review"] is False                                              # coding is under way: no pull request yet
    jobs[0].d["push"] = {"done": True}
    assert progress.flags("r1", True) == {"em2": True, "review": True, "qa": False}                   # a branch is pushed: the Code reviewer
    jobs[0].d["merged"] = True
    assert progress.flags("r1", True) == {"em2": True, "review": True, "qa": True}                    # merged: the QA agent
    assert progress.flags("other-run", True)["review"] is False                                        # another session's jobs do not unlock this one
    jobs[0].d["merged"] = False
    loop.STORE.mkdir(parents=True)
    (loop.STORE / "o__r__5.json").write_text(json.dumps({"phase": "merged", "branch": "feat", "repo": ["O", "R"]}))                    # merged from the reviewer
    assert progress.flags("r1", True)["qa"] is True
    (loop.STORE / "o__r__5.json").write_text(json.dumps({"phase": "awaiting_fix", "branch": "feat", "repo": ["o", "r"]}))
    assert progress.flags("r1", True)["qa"] is False                                                   # an unmerged conversation does not


def test_engineering_manager_2_sees_only_the_tasks_of_the_open_session(monkeypatch, tmp_path):
    import json
    import config
    import progress
    from coding import tasks as coding_tasks
    monkeypatch.setattr(config, "RUNS_ROOT", tmp_path)
    plan_a = {"epic": {"title": "Coupons"}, "stories": [{"id": "S1", "title": "Redeem", "tasks": [
        {"title": "Add redeem endpoint", "description": "d", "assignment": {"agent": "coding_java"}, "subtasks": [{"title": "Entity", "description": "x"}]},
        {"title": "Add verify service", "description": "d", "assignment": {"agent": "coding_java"}}]}]}
    plan_b = {"epic": {"title": "Other"}, "stories": [{"id": "S1", "title": "Else", "tasks": [{"title": "A task from another session", "description": "d",
                                                                                              "assignment": {"agent": "coding_python"}}]}]}
    (tmp_path / "run-A").mkdir()
    (tmp_path / "run-A" / "jira_created.json").write_text(json.dumps({"created": {"S1/T1": "ABC-1"}}))
    (tmp_path / "run-B").mkdir()
    (tmp_path / "run-B" / "session.json").write_text(json.dumps({"pipeline_state": {"release_plan": plan_b}}))          # another session, saved on disk
    monkeypatch.setattr(coding_tasks, "list_tasks", lambda *a, **k: pytest.fail("the saved tasks of every run must never be read for this"))
    got = progress.session_tasks(plan_a, "run-A")
    assert [(t.run_id, t.title, t.jira_key) for t in got] == [("run-A", "Add redeem endpoint", "ABC-1"), ("run-A", "Add verify service", None)]
    assert [s["title"] for s in got[0].subtasks] == ["Entity"] and not any("another session" in t.title for t in got)
    assert progress.session_tasks({}, "run-A") == [] and progress.session_tasks(plan_a, "") == [] and progress.session_tasks({"stories": []}, "run-A") == []
