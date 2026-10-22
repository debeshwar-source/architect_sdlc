"""Checks of the coding agent (coding/). Run: pytest test_coding.py

Real Git repositories in a temp folder; the AI and GitHub are never called.
"""

import json
import subprocess
from pathlib import Path

import pytest

from coding import agent, tasks, workspace


def sh(cwd, *args):
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def no_real_jira(monkeypatch):
    for name in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY", "JIRA_EMAIL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORK_ROOT", tmp_path / "work")
    from coding import memory
    monkeypatch.setattr(memory, "MEMORY_ROOT", tmp_path / "memory")
    r = tmp_path / "app"
    r.mkdir()
    sh(r, "init", "-q", "-b", "main")
    (r / "a.py").write_text("print(1)\n")
    sh(r, "add", "."); sh(r, "commit", "-q", "-m", "init")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T"); monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T"); monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    return r


def make_run(root: Path, name="20260101-000000-aaaaaa"):
    run = root / name
    run.mkdir(parents=True)
    plan = {"epic": {"title": "E"}, "stories": [{"id": "S1", "title": "St", "acceptance_criteria": ["ac"], "tasks": [
        {"title": "Do it", "description": "desc", "assignment": {"agent": "coding_python", "kb_refs": [],
         "output": {"summary": "sum", "branch": "feature/x", "paths": ["a.py"], "done_checks": ["works"]}},
        "subtasks": [{"title": "Add one", "description": "create one.py"}, {"title": "Add two", "description": "create two.py"},
                     {"title": " "}, {"title": "Add three"}]}]}]}
    (run / "session.json").write_text(json.dumps({"completed_events": [["release", {"release_plan": plan}]]}))
    (run / "jira_created.json").write_text(json.dumps({"created": {"S1/T1": "PRJ-7"}}))
    return run


def test_tasks_are_listed_with_jira_key(tmp_path):
    make_run(tmp_path)
    (tmp_path / "empty").mkdir(); (tmp_path / "empty" / "session.json").write_text("{}")
    found = tasks.list_tasks(tmp_path)
    assert len(found) == 1 and found[0].jira_key == "PRJ-7" and found[0].agent == "coding_python"
    text = tasks.briefing(found[0])
    assert "Do it" in text and "works" in text and "ac" in text


def test_agent_works_in_a_separate_branch_and_commit_leaves_user_copy_alone(repo):
    w = workspace.prepare(str(repo), "run:S1/T1", "feature/x", "Do it")
    assert w.branch == "feature/x" and w.base == "main" and w.tree != repo
    (w.tree / "a.py").write_text("print(2)\n")
    (w.tree / "new.py").write_text("x = 1\n")
    (w.tree / "__pycache__").mkdir(); (w.tree / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"x")
    assert set(workspace.changed_files(w)) == {"a.py", "new.py"}
    assert "print(2)" in workspace.diff(w) and "new.py" in workspace.diff(w)
    workspace.commit(w, "Do it")
    assert "pyc" not in subprocess.run(["git", "show", "--stat", "HEAD"], cwd=w.tree, capture_output=True, text=True).stdout
    assert (repo / "a.py").read_text() == "print(1)\n"                      # the user's folder is untouched
    assert workspace.changed_files(w)                                        # still listed after the commit
    # a second run of the same task gets a fresh branch
    assert workspace.prepare(str(repo), "run:S1/T1", "feature/x", "Do it").branch == "feature/x-2"


def test_nothing_to_commit_is_an_error(repo):
    w = workspace.prepare(str(repo), "k", "", "Title")
    with pytest.raises(workspace.GitError):
        workspace.commit(w, "x")


def test_not_a_repository(tmp_path):
    with pytest.raises(workspace.GitError):
        workspace.prepare(str(tmp_path), "k", "", "t")


def test_pull_request_pushes_and_gives_compare_link_without_token(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False); monkeypatch.delenv("GH_TOKEN", raising=False)
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare))
    sh(repo, "remote", "add", "origin", str(bare))
    w = workspace.prepare(str(repo), "k", "feature/x", "Do it")
    (w.tree / "a.py").write_text("print(3)\n")
    workspace.commit(w, "Do it")
    out = workspace.open_pull_request(w, "Do it", "body")
    assert out["opened"] is False and "feature/x" in subprocess.run(["git", "branch", "--list"], cwd=bare, capture_output=True, text=True).stdout


def test_github_repo_parsing_and_branch_names():
    assert workspace.github_repo("git@github.com:org/repo.git") == ("org", "repo")
    assert workspace.github_repo("https://github.com/org/repo") == ("org", "repo")
    assert workspace.github_repo("/tmp/x.git") is None
    assert workspace.is_url("https://github.com/o/r.git") and not workspace.is_url("/Users/me/repo")
    assert workspace.branch_name("bad name!!", "t") == "bad-name" and workspace.branch_name("", "Add Login!") == "feature/add-login"


def test_pr_body_has_no_tool_attribution():
    t = tasks.CodingTask("r", "S1", 1, "T", "d", "coding_python", assignment={"output": {"done_checks": ["c"]}}, jira_key="PRJ-1")
    body = workspace.pr_body(t, {"summary": "did", "assumptions": ["a"], "checks_run": "pytest"})
    assert "PRJ-1" in body and "did" in body and "Claude" not in body and "Co-Authored" not in body


def test_agent_answer_cleaning():
    q = agent.clean({"status": "question", "summary": "s", "question": {"question": " Which? ", "options": ["a", "b"], "recommended_option": "zzz"}})
    assert q["status"] == "question" and q["question"]["recommended_option"] == "a"
    assert agent.clean({"status": "question", "summary": "s", "question": {"question": ""}})["status"] == "done"
    assert agent.clean({"status": "done"})["summary"] == ""


def test_system_prompts_contain_skill_and_rules():
    plan, run = agent.system_prompt("coding_python", agent.PLAN_RULES), agent.system_prompt("coding_python", agent.EXEC_RULES)
    assert "Python" in plan and "cannot edit" in plan and "Engineering Manager 2" in plan
    assert "Do not read the whole" in plan
    assert "Python" in run and "Never commit, push" in run and "a shell (Bash)" in run
    assert "Edit" not in agent.READ_ONLY and "Bash" not in agent.READ_ONLY


def test_the_plan_with_assignments_wins_over_a_later_lookalike(tmp_path):
    run = make_run(tmp_path)
    data = json.loads((run / "session.json").read_text())
    plan = data["completed_events"][0][1]["release_plan"]
    bare = {"epic": {"title": "E"}, "stories": [{"id": "S1", "tasks": [{"title": "Do it"}]}]}      # like the Jira summary
    data["completed_events"].append(["jira_publish", {"jira_result": bare}])
    (run / "session.json").write_text(json.dumps(data))
    found = tasks.tasks_of_run(run)
    assert found and found[0].agent == "coding_python" and found[0].assignment == plan["stories"][0]["tasks"][0]["assignment"]


def test_base_branch_typed_with_wrong_case_is_matched(repo):
    sh(repo, "branch", "dev")
    assert workspace.prepare(str(repo), "k1", "", "t", base="Dev").base == "dev"
    with pytest.raises(workspace.GitError, match="no branch 'nope'.*dev"):
        workspace.prepare(str(repo), "k2", "", "t", base="nope")


# ---------- plan cleaning ----------

def test_manager2_plan_cleaning():
    p = agent.clean_plan({"status": "plan", "summary": "s", "repo_notes": " mvn test ", "steps": [
        {"title": "A", "agent": "coding_java", "instructions": "do a", "changes": [{"path": "x.java", "action": "create", "what": "w"}, {"path": " "}, "junk"],
         "context": "c", "tests": "mvn -q test", "check": "ok"},
        {"title": " "},
        {"title": "B", "agent": "nobody", "detail": "old-style"}]}, default_agent="coding_python")
    a, b = p["steps"]
    assert (a["n"], a["agent"], a["detail"], a["files"]) == (1, "coding_java", "do a", ["x.java (create)"]) and a["changes"][0]["what"] == "w"
    assert (b["n"], b["agent"], b["detail"]) == (2, "coding_python", "old-style") and p["repo_notes"] == "mvn test"   # unknown expert -> the default
    q = agent.clean_plan({"status": "question", "question": {"question": "Which?", "options": ["a"]}})
    assert q["status"] == "question" and q["question"]["recommended_option"] == "a"
    with pytest.raises(agent.Incomplete):
        agent.clean_plan({"status": "plan", "steps": []})
    learned = agent.clean_plan({"status": "plan", "steps": [{"title": "A"}], "learnings": [{"text": " Uses  Maven. ", "files": ["pom.xml", " "]}, {"text": ""}, "junk"]})["learnings"]
    assert learned == [{"text": "Uses Maven.", "files": ["pom.xml"]}]
    assert len(agent.clean_plan({"status": "plan", "steps": [{"title": str(i)} for i in range(30)]})["steps"]) == agent.MAX_STEPS


def test_step_prompt_carries_everything_the_coder_needs():
    step = {"n": 2, "title": "Add endpoint", "agent": "coding_java", "detail": "Create CouponController.", "context": "Copy PaymentController.",
            "changes": [{"path": "a/Coupon.java", "action": "create", "what": "controller"}], "tests": "mvn -q test", "check": "200 on valid"}
    text = agent.step_prompt([{"n": 1, "title": "Model", "status": "approved"}, step], step, "BRIEFING", None, "Build with mvn.")
    for needle in ("BRIEFING", "REPOSITORY NOTES", "Build with mvn.", "1. Model [done]", "Create CouponController.", "create a/Coupon.java: controller",
                   "Copy PaymentController.", "mvn -q test", "200 on valid"):
        assert needle in text, needle
    assert "BRIEFING" not in agent.step_prompt([step], step, None, None)         # a continuing conversation already has it


# ---------- the side-by-side diff ----------

DIFF = """diff --git a/a.py b/a.py
index 111..222 100644
--- a/a.py
+++ b/a.py
@@ -1,4 +1,5 @@
 line1
-old2
+new2
+extra
 line3
 line4
@@ -20,2 +21,2 @@ def f():
 keep
-gone
+<b>&amp;</b>
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+world
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1,1 +0,0 @@
-bye
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
"""


def test_diff_is_parsed_into_side_by_side_rows():
    from coding import diffview
    files = diffview.parse(DIFF)
    assert [(f.path, f.status) for f in files] == [("a.py", "modified"), ("new.txt", "added"), ("old.txt", "deleted"), ("img.png", "binary")]
    a = files[0]
    assert (a.added, a.removed) == (3, 2)
    assert ("chg", 2, "old2", 2, "new2") in a.rows                  # a replaced line sits on one row
    assert ("add", None, "", 3, "extra") in a.rows                   # an extra line has nothing opposite
    assert any(r[0] == "sep" for r in a.rows)                        # the gap between the two hunks is marked
    assert ("ctx", 1, "line1", 1, "line1") in a.rows
    assert files[1].rows[0] == ("add", None, "", 1, "hello") and files[2].rows[0] == ("del", 1, "bye", None, "")


def test_diff_html_is_escaped_and_coloured():
    from coding import diffview
    out = diffview.render(diffview.parse(DIFF))
    assert "&lt;b&gt;&amp;amp;&lt;/b&gt;" in out and "<b>" not in out.split("</style>")[1]      # file text can never inject markup
    assert 'class="t del l"' in out and 'class="t add r"' in out and "Binary file" in out
    assert "No changes" in diffview.render([]) and 80 <= diffview.height(diffview.parse(DIFF)) <= 900


def test_diff_of_a_real_change(repo):
    from coding import diffview
    w = workspace.prepare(str(repo), "k", "", "t")
    (w.tree / "a.py").write_text("print(2)\n")
    (w.tree / "b.py").write_text("x = 1\n")
    files = diffview.parse(workspace.diff(w))
    assert {f.path: f.status for f in files} == {"a.py": "modified", "b.py": "added"}


# ---------- the job: plan -> freeze -> steps -> approve -> pull request ----------

CONFIRM_TESTS = {"on": False}      # the older tests run the tests straight away; the "wait for approval" tests turn this on


@pytest.fixture(autouse=True)
def _tests_run_straight_away_unless_asked(monkeypatch):
    from coding import flow
    CONFIRM_TESTS["on"] = False
    real = flow.new_job

    def new_job(*a, **k):
        job = real(*a, **k)
        job.d["confirm_tests"] = bool(CONFIRM_TESTS["on"] or job.d.get("confirm_tests"))
        job.save()
        return job
    monkeypatch.setattr(flow, "new_job", new_job)


def _s(n, title, agent_id="coding_python"):
    return {"n": n, "title": title, "agent": agent_id, "detail": "d", "changes": [], "files": [f"x{n}.py (create)"], "context": "ctx", "tests": "pytest", "check": "c"}


PLAN = {"status": "plan", "summary": "two steps", "repo_notes": "run pytest",
        "learnings": [{"text": "Tests run with pytest from the repo root.", "files": ["a.py"]}, {"text": "Naming: snake_case modules.", "files": []}], "steps": [_s(1, "Add one"), _s(2, "Add two")]}


class FakeAgent:
    """Stands in for the AI: plans two steps; each step writes a file."""

    def __init__(self, monkeypatch, ask_first=False):
        self.prompts, self.memories, self.ask_first = [], [], ask_first
        monkeypatch.setattr(agent, "decompose", self.plan)
        monkeypatch.setattr(agent, "execute", self.execute)

    def plan(self, agent_id, briefing, cwd, model=None, resume=None, user_input=None, on_event=None, current_steps=None, memory=""):
        self.prompts.append(("plan", user_input))
        self.memories.append(memory)
        if self.ask_first and not user_input:
            return {"session_id": "p1", "answer": {"status": "question", "summary": "hm", "steps": [],
                    "question": {"question": "Which db?", "options": ["pg", "mysql"], "recommended_option": "pg"}}}
        steps = PLAN["steps"] if not user_input or "split" not in user_input else PLAN["steps"] + [_s(3, "Add three")]
        return {"session_id": "p1", "answer": {**PLAN, "steps": steps}}

    def execute(self, agent_id, prompt, cwd, model=None, resume=None, on_event=None):
        self.prompts.append(("exec", prompt))
        import re
        n = re.search(r"(?:STEP|step) (\d)", prompt).group(1)          # the step this prompt is about
        name = {"1": "one.py", "2": "two.py", "3": "three.py"}[n]
        Path(cwd, name).write_text(f"# {len(self.prompts)}\n")
        if on_event:
            on_event("Write " + name)
        return {"session_id": "e1", "answer": {"status": "done", "summary": f"wrote {name}", "checks_run": "ok", "assumptions": []}}


@pytest.fixture
def job(repo, monkeypatch, tmp_path):
    from coding import flow
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    fake = FakeAgent(monkeypatch)
    task = tasks.tasks_of_run(make_run(tmp_path / "runs"))[0]
    j = flow.new_job(task, "coding_python", str(repo), "", "m")
    j.fake = fake
    return j


def test_full_flow_plan_freeze_steps_commits_and_pr(job, repo, tmp_path, monkeypatch):
    from coding import flow
    monkeypatch.delenv("GITHUB_TOKEN", raising=False); monkeypatch.delenv("GH_TOKEN", raising=False)
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare)); sh(repo, "remote", "add", "origin", str(bare))

    flow.plan_turn(job)
    assert [s["title"] for s in job.steps] == ["Add one", "Add two"] and job.phase == "planning"
    flow.plan_turn(job, user_input="please split things")                       # reconsider
    assert len(job.steps) == 3
    with pytest.raises(ValueError):
        flow.execute_step(job, 1)                                                # not frozen yet -> no phase move, but plan must be frozen first
    flow.freeze_plan(job)
    assert job.phase == "executing" and job.d["plan"]["frozen"] and job.d["jira"]["status"] == "unavailable"     # Jira is not configured here
    with pytest.raises(ValueError):
        flow.plan_turn(job)                                                      # a frozen plan cannot be re-planned

    with pytest.raises(ValueError):
        flow.execute_step(job, 2)                                                # steps go in order
    flow.execute_step(job, 1)
    s1 = job.step(1)
    assert s1["status"] == "review" and "one.py" in workspace.changed_files(job.workspace, s1["start_sha"])
    flow.revise_step(job, 1, "use a different comment")                          # reconsider: redone from the step's start
    assert job.step(1)["status"] == "review" and job.fake.prompts[-1][0] == "exec"
    flow.approve_step(job, 1, "Step one done")
    assert job.step(1)["status"] == "approved" and len(workspace.commits(job.workspace)) == 1

    flow.execute_step(job, 2); flow.skip_step(job, 2)                            # skipped: its edits vanish
    assert not (job.workspace.tree / "two.py").exists() and job.step(2)["status"] == "skipped"
    flow.execute_step(job, 3); flow.approve_step(job, 3, "Step three")
    assert job.phase == "final"

    final = flow.final_summary(job)
    assert [c["subject"] for c in final["commits"]] == ["Step one done", "Step three"] and set(final["files"]) == {"one.py", "three.py"}
    out = flow.raise_pr(job, "T", flow.pr_description(job), squash_message="Everything")
    assert out["opened"] is False and job.phase == "done"
    assert len(workspace.commits(job.workspace)) == 1                            # squashed
    assert flow.load(job.id).phase == "done" and (repo / "one.py").exists() is False   # saved; user's folder untouched


def test_question_during_planning_and_saved_job_reload(repo, monkeypatch, tmp_path):
    from coding import flow
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    FakeAgent(monkeypatch, ask_first=True)
    task = tasks.tasks_of_run(make_run(tmp_path / "runs"))[0]
    j = flow.new_job(task, "coding_python", str(repo), "", "m")
    flow.plan_turn(j)
    assert j.d["question"]["for"] == "plan" and not j.steps
    again = flow.load(j.id)                                                      # a closed app picks the job up again
    assert again.d["question"]["question"] == "Which db?" and flow.list_jobs(task.key)[0].id == j.id
    flow.answer_question(again, "pg")
    assert again.d["question"] is None and len(again.steps) == 2


def test_errors_are_kept_and_continue_resumes(job, monkeypatch):
    from coding import flow
    calls = []

    def boom(*a, **k):
        calls.append(k.get("resume"))
        raise agent.Incomplete("ran out of turns", "sess-9")
    flow.plan_turn(job)
    flow.freeze_plan(job)
    monkeypatch.setattr(agent, "execute", boom)
    flow.execute_step(job, 1)
    assert "ran out of turns" in job.d["error"] and job.d["exec_sessions"]["coding_python"] == "sess-9" and job.step(1)["status"] == "pending"
    flow.continue_after_error(job)
    assert calls == [None, "sess-9"]                                             # the second try resumed the session


def test_pending_plan_can_be_reopened_only_before_work_starts(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    flow.unfreeze_plan(job)
    assert job.phase == "planning"
    flow.freeze_plan(job); flow.execute_step(job, 1)
    with pytest.raises(ValueError):
        flow.unfreeze_plan(job)


# ---------- the screen, end to end (the AI is faked) ----------

def _app():
    import os
    from pathlib import Path
    from coding import tasks, ui
    ui.render(tasks.tasks_of_run(Path(os.environ["CODING_TEST_RUN"])))


def _click(at, label):
    button = next(b for b in at.button if b.label == label)
    button.click()
    return at.run()


def test_screen_walks_through_manager2_subtasks_and_pull_request(repo, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from coding import flow
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False); monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("CODING_TEST_RUN", str(make_run(tmp_path / "runs")))
    FakeAgent(monkeypatch)
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare)); sh(repo, "remote", "add", "origin", str(bare))

    at = AppTest.from_function(_app, default_timeout=60).run()
    assert not at.exception
    at.sidebar.selectbox[0].select_index(0)
    at.run()
    at.sidebar.text_input[0].set_value(str(repo))
    at.run()
    at = _click(at, "Start a new job")
    assert not at.exception, at.exception
    assert any("Engineering Manager 2" in m.value for m in at.markdown), "Engineering Manager 2 broke the task down"
    assert any("Add one" in m.value for m in at.markdown)
    heads = lambda: [b.label.split("  ", 1)[1] for b in at.button if b.key and b.key.endswith("_head")]
    assert heads() == ["🧭 Engineering Manager 2"], "the coding agent appears only once the subtasks are frozen"
    assert [b.label[0] for b in at.button if b.key and b.key.endswith("_head")] == ["▸"], "and the manager's box starts closed"
    labels = lambda: [b.label for b in at.button]
    assert not any(b.label.startswith("Execute step") for b in at.button)
    at = _click(at, "Freeze the subtasks")
    assert not at.exception, at.exception
    assert heads() == ["🧭 Engineering Manager 2", "🛠️ Coding agent"], "now it follows the manager"
    for n in (1, 2):
        at = _click(at, f"Execute step {n}")
        assert not at.exception, at.exception
        assert any(f"Step {n} is finished" in s.value for s in at.success)
        assert not any(b.label == "Commit this step" for b in at.button), "the diff comes first, then the commit"
        at = _click(at, "Approve the changes")
        assert not at.exception, at.exception
        assert any(b.label == "Commit this step" for b in at.button)
        at = _click(at, "Commit this step")
        undo = next(b for b in at.button if b.label == "↶ Undo the commit of step 1")          # the record on show is step 1's
        assert undo.disabled is (n == 2), "a commit can be undone only while nothing was done after it"
    assert any("Push and pull request" in m.value for m in at.markdown)
    assert not any(b.label == "Open the pull request" for b in at.button), "the pull request waits for the push"
    assert "feature/x" not in subprocess.run(["git", "branch", "--list"], cwd=bare, capture_output=True, text=True).stdout     # nothing pushed yet
    at = _click(at, "Push the branch")
    assert not at.exception, at.exception
    assert any("Pushed" in s.value for s in at.success)
    at = _click(at, "Open the pull request")
    assert not at.exception, at.exception
    assert "feature/x" in subprocess.run(["git", "branch", "--list"], cwd=bare, capture_output=True, text=True).stdout


# ---------- starting from the manager's subtasks ----------

def test_subtasks_are_carried_and_blank_ones_dropped(tmp_path):
    t = tasks.tasks_of_run(make_run(tmp_path))[0]
    assert [s["title"] for s in t.subtasks] == ["Add one", "Add two", "Add three"] and t.subtasks[0]["description"] == "create one.py"


def test_chosen_subtasks_become_the_frozen_steps_with_no_planning(repo, monkeypatch, tmp_path):
    from coding import flow
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    fake = FakeAgent(monkeypatch)
    task = tasks.tasks_of_run(make_run(tmp_path / "runs"))[0]
    j = flow.new_job(task, "coding_python", str(repo), "", "m", subtasks=[2, 0])           # picked out of order
    assert j.phase == "executing" and j.d["plan"]["frozen"] and fake.prompts == []         # the AI was not asked to plan
    assert [(s["n"], s["title"], s["detail"]) for s in j.steps] == [(1, "Add three", ""), (2, "Add one", "create one.py")]
    # the old way still works for a task without subtasks (or none picked)
    assert flow.new_job(task, "coding_python", str(repo), "", "m").phase == "planning"


def test_skipped_steps_are_named_to_the_agent():
    steps = [{"n": 1, "title": "A", "status": "skipped"}, {"n": 2, "title": "B", "status": "approved"}, {"n": 3, "title": "C", "status": "pending"}]
    text = agent.step_prompt(steps, steps[2], None, None)
    assert "except step(s) 1" in text and "NOT in the code" in text
    assert "except" not in agent.step_prompt([dict(s, status="approved") for s in steps], steps[2], None, None)


def test_string_subtasks_do_not_break_task_listing(tmp_path):
    run = make_run(tmp_path)
    data = json.loads((run / "session.json").read_text())
    plan = data["completed_events"][0][1]["release_plan"]
    plan["stories"][0]["tasks"][0]["subtasks"] = ["Just a title", {"title": "With text", "description": "d"}, 5, {"title": ""}]
    (run / "session.json").write_text(json.dumps(data))
    assert [s["title"] for s in tasks.tasks_of_run(run)[0].subtasks] == ["Just a title", "With text", "5"]


def test_screen_can_use_the_first_managers_subtasks_instead(repo, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from coding import flow
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setenv("CODING_TEST_RUN", str(make_run(tmp_path / "runs")))
    fake = FakeAgent(monkeypatch)
    at = AppTest.from_function(_app, default_timeout=60).run()
    at.sidebar.selectbox[0].select_index(0); at.run()
    at.sidebar.radio[0].set_value("The first manager's subtasks, as they are"); at.run()
    assert at.sidebar.multiselect and at.sidebar.multiselect[0].label.startswith("Subtasks to execute")
    at.sidebar.text_input[0].set_value(str(repo)); at.run()
    at = _click(at, "Start a new job")
    assert not at.exception, at.exception
    assert fake.prompts == [] and any(b.label == "Execute step 1" for b in at.button)         # nobody was asked to plan


# ---------- Jira ----------

class FakeSession:
    """A stand-in for the network: remembers the issue's description."""

    def __init__(self, description):
        self.description, self.calls = description, []

    def request(self, method, url, json=None, **kw):
        self.calls.append((method, url.split("/rest/api/2")[1], json))
        body = {"fields": {"description": self.description}}
        if method == "PUT":
            self.description = json["fields"]["description"]
        return type("R", (), {"status_code": 204 if method == "PUT" else 200, "headers": {}, "json": lambda s: body, "text": ""})()


def test_jira_steps_replace_only_the_steps_section_and_add_agent_labels():
    from tools import jira_client
    from coding import jira_sync
    steps = [{"n": 1, "title": "Model", "agent": "coding_java", "detail": "Add entity.\nWith id.", "files": ["A.java (create)"], "check": "mvn test"},
             {"n": 2, "title": "UI", "agent": "coding_web", "detail": "", "files": [], "check": ""}]
    session = FakeSession("Do the thing.\n\nh3. Steps\n* old a\n* old b\n\nh3. Assignment\n* agent: x\n\nfooter")
    client = jira_client.JiraClient("https://j.example", "P", "tok", session=session)
    task = tasks.CodingTask("r", "S1", 1, "T", "d", "coding_java", jira_key="P-5")
    out = jira_sync.update(task, steps, client=client)
    assert out["ok"] and "P-5" in out["note"]
    assert session.description.startswith("Do the thing.") and "old a" not in session.description
    assert "# *Model* [coding_java]" in session.description and "#* Files: A.java (create)" in session.description
    assert "h3. Assignment\n* agent: x\n\nfooter" in session.description                           # the rest is untouched
    put = next(c for c in session.calls if c[0] == "PUT")
    assert {"add": "agent-coding_java"} in put[2]["update"]["labels"] and {"add": "agent-coding_web"} in put[2]["update"]["labels"]
    assert "\n" not in jira_sync.step_lines(steps)[1]                                                  # one line per list item


def test_jira_is_only_offered_when_it_can_work(monkeypatch):
    from coding import jira_sync
    for name in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"):
        monkeypatch.delenv(name, raising=False)
    task = tasks.CodingTask("r", "S1", 1, "T", "d", "coding_java", jira_key="P-5")
    assert jira_sync.availability(task)[0] is False and "not configured" in jira_sync.availability(task)[1]
    for name in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"):
        monkeypatch.setenv(name, "x")
    assert jira_sync.availability(task) == (True, "")
    assert "no Jira ticket" in jira_sync.availability(tasks.CodingTask("r", "S1", 1, "T", "d", "coding_java"))[1]


def test_freezing_asks_before_touching_jira_and_nothing_changes_until_yes(job, monkeypatch):
    from coding import flow, jira_sync
    monkeypatch.setattr(jira_sync, "availability", lambda task: (True, ""))
    sent = []
    monkeypatch.setattr(jira_sync, "update", lambda task, steps, client=None: sent.append(len(steps)) or {"ok": True, "key": "P-5", "note": "P-5 now lists 2 step(s)."})
    flow.plan_turn(job); flow.freeze_plan(job)
    assert job.phase == "jira" and sent == []                                  # it asks first
    flow.update_jira(job)
    assert sent == [2] and job.phase == "executing" and job.d["jira"]["status"] == "updated"
    with pytest.raises(ValueError, match="Jira has already"):
        flow.unfreeze_plan(job)                                                # Jira now matches the subtasks, so they stay frozen
    # saying no leaves Jira alone and carries on
    j2 = flow.new_job(job.task, "coding_python", str(job.workspace.repo), "", "m")
    flow.plan_turn(j2); flow.freeze_plan(j2); flow.skip_jira(j2)
    assert sent == [2] and j2.phase == "executing" and j2.d["jira"]["status"] == "skipped"


def test_a_failed_jira_update_keeps_the_question_open(job, monkeypatch):
    from coding import flow, jira_sync
    monkeypatch.setattr(jira_sync, "availability", lambda task: (True, ""))
    monkeypatch.setattr(jira_sync, "update", lambda task, steps, client=None: {"ok": False, "key": "P-5", "note": "404 not found"})
    flow.plan_turn(job); flow.freeze_plan(job); flow.update_jira(job)
    assert job.phase == "jira" and job.d["jira"] == {"status": "failed", "note": "404 not found"}


def test_each_subtask_runs_with_its_own_expert_and_session(job, monkeypatch):
    from coding import flow
    seen = []
    real = agent.execute
    monkeypatch.setattr(agent, "execute", lambda a, prompt, cwd, model=None, resume=None, on_event=None: (seen.append((a, resume)), real(a, prompt, cwd, model, resume, on_event))[1])
    flow.plan_turn(job)
    flow.set_agents(job, {2: "coding_web"})
    flow.freeze_plan(job)
    flow.execute_step(job, 1); flow.approve_step(job, 1, "one")
    flow.execute_step(job, 2)
    assert [a for a, _ in seen] == ["coding_python", "coding_web"] and seen[1][1] is None          # a new expert starts a new conversation
    assert set(job.d["exec_sessions"]) == {"coding_python", "coding_web"}


def test_screen_asks_before_updating_jira_and_only_then_continues(repo, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from coding import flow, jira_sync
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setenv("CODING_TEST_RUN", str(make_run(tmp_path / "runs")))
    monkeypatch.setattr(jira_sync, "availability", lambda task: (True, ""))
    sent = []
    monkeypatch.setattr(jira_sync, "update", lambda task, steps, client=None: sent.append(1) or {"ok": True, "key": "PRJ-7", "note": "PRJ-7 now lists 2 step(s)."})
    FakeAgent(monkeypatch)
    at = AppTest.from_function(_app, default_timeout=60).run()
    at.sidebar.selectbox[0].select_index(0); at.run()
    at.sidebar.text_input[0].set_value(str(repo)); at.run()
    at = _click(at, "Start a new job")
    at = _click(at, "Freeze the subtasks")
    assert not at.exception, at.exception
    assert any("Update Jira" in m.value for m in at.markdown) and sent == []          # asked, not done
    assert "🛠️ Coding agent" not in [s.value for s in at.subheader]                   # still the manager's turn
    assert not any(b.label == "Execute step 1" for b in at.button)                      # coding waits for the answer
    at = _click(at, "Yes, update Jira")
    assert sent == [1] and any(b.label == "Execute step 1" for b in at.button)


def test_screen_shows_where_the_job_is():
    from coding import ui
    assert [label for _, label in ui.STAGES][0] == "🧭 Engineering Manager 2" and "Engineering Manager 2" in ui.WHAT


# ---------- describe, do not write ----------

def test_code_inside_subtasks_is_measured():
    step = {"detail": "Create it:\n```java\nclass A {\n}\n```\nthen:\n```\nx\n```", "context": "```\n" + "l\n" * 20 + "```"}
    assert agent.code_lines(step) == 3 + 20 and agent.code_lines({"detail": "words only", "context": ""}) == 0


def test_a_manager_that_writes_code_is_noted_but_not_sent_back(monkeypatch):
    heavy = {"status": "plan", "summary": "s", "steps": [{"title": "A", "agent": "coding_python", "instructions": "```\n" + "code\n" * 40 + "```"}]}
    assert agent.too_detailed(agent.clean_plan(dict(heavy))["steps"][0])
    calls, events = [], []

    def fake_call(label, agent_id, prompt, system, cwd, model, resume, on_event, schema, tools, disallowed, max_turns=80):
        calls.append(label)
        return {"session_id": "m1", "answer": dict(heavy)}
    monkeypatch.setattr(agent, "_call", fake_call)
    out = agent.decompose("coding_python", "brief", "/tmp", on_event=events.append)
    assert calls == ["manager2"]                                    # one planning turn only: no costly redo
    assert out["answer"]["steps"] and any("more detailed than asked" in e for e in events)


def test_manager_rules_say_describe_and_remember():
    rules = agent.PLAN_RULES
    assert "WHAT and WHERE" in rules and "HOW" in rules and "WHAT YOU REMEMBER" in rules and "learnings" in rules


# ---------- the manager's memory of a repository ----------

def test_memory_remembers_dedupes_and_forgets(repo):
    from coding import memory
    w = workspace.prepare(str(repo), "k", "", "t")
    key = memory.key_for(w)
    assert memory.remember(key, str(repo), w.base_commit, [{"text": "Tests run with pytest.", "files": ["a.py"]}, {"text": "", "files": []}]) == 1
    assert memory.remember(key, str(repo), w.base_commit, [{"text": "tests run with pytest!", "files": ["a.py"]}, {"text": "Use snake_case.", "files": []}]) == 1   # same fact refreshed
    assert [f["text"] for f in memory.load(key)["facts"]] == ["tests run with pytest!", "Use snake_case."]
    long_a = "Controllers return ResponseEntity and map errors through the shared exception handler class"
    memory.remember(key, str(repo), w.base_commit, [{"text": long_a, "files": ["a/Ctl.java"]}])
    memory.remember(key, str(repo), w.base_commit, [{"text": long_a + "s", "files": ["b/Other.java"]}])        # near-identical words about other files: a different fact
    memory.remember(key, str(repo), w.base_commit, [{"text": long_a + "s", "files": ["a/Ctl.java"]}])          # ... about the same files: the same fact, refreshed
    assert len(memory.load(key)["facts"]) == 4
    memory.forget(key, 3); memory.forget(key, 2)
    memory.forget(key, 0)
    assert [f["text"] for f in memory.load(key)["facts"]] == ["Use snake_case."]
    memory.forget(key)
    assert memory.load(key)["facts"] == [] and memory.for_prompt(key, w) == ""
    for i in range(memory.MAX_FACTS + 5):
        memory.remember(key, str(repo), w.base_commit, [{"text": f"fact number {i} about topic {i * 7}", "files": [f"f{i}.py"]}])
    assert len(memory.load(key)["facts"]) == memory.MAX_FACTS


def test_memory_marks_facts_whose_files_changed_since(repo):
    from coding import memory
    (repo / "b.py").write_text("b = 1\n"); sh(repo, "add", "."); sh(repo, "commit", "-q", "-m", "add b")
    w1 = workspace.prepare(str(repo), "k1", "", "t")                    # the facts are learned with b.py already there
    key = memory.key_for(w1)
    memory.remember(key, str(repo), w1.base_commit, [{"text": "about a", "files": ["a.py"]}, {"text": "about b", "files": ["b.py"]}, {"text": "general", "files": []}])
    (repo / "a.py").write_text("print(99)\n"); sh(repo, "add", "."); sh(repo, "commit", "-q", "-m", "change a")       # a.py changes, b.py does not
    w2 = workspace.prepare(str(repo), "k2", "", "t")
    stale = {f["text"]: f["stale"] for f in memory.facts(key, w2)}
    assert stale == {"about a": True, "about b": False, "general": False}
    text = memory.for_prompt(key, w2)
    assert "CHECK" in text.splitlines()[0] and "fresh" in text.splitlines()[1] and "(see a.py)" in text


def test_frozen_subtasks_are_remembered_and_the_next_job_is_given_them(repo, monkeypatch, tmp_path):
    from coding import flow, memory
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    fake = FakeAgent(monkeypatch)
    task = tasks.tasks_of_run(make_run(tmp_path / "runs"))[0]
    j1 = flow.new_job(task, "coding_python", str(repo), "", "m")
    flow.plan_turn(j1)
    assert fake.memories[-1] == "" and memory.load(memory.key_for(j1.workspace))["facts"] == []        # nothing yet; a plan alone is not remembered
    flow.freeze_plan(j1)
    assert j1.d["memory_added"] == 2
    j2 = flow.new_job(task, "coding_python", str(repo), "", "m")                                       # the next job, same repository
    flow.plan_turn(j2)
    assert "Tests run with pytest from the repo root." in fake.memories[-1] and "fresh" in fake.memories[-1]
    other = tmp_path / "other"; other.mkdir(); sh(other, "init", "-q", "-b", "main"); (other / "x").write_text("x"); sh(other, "add", "."); sh(other, "commit", "-q", "-m", "i")
    j3 = flow.new_job(task, "coding_python", str(other), "", "m")
    flow.plan_turn(j3)
    assert fake.memories[-1] == ""                                                                     # another repository: nothing carried over


# ---------- the manager must not do half the coder's job ----------

LIKE_THE_REAL_ONE = {
    "detail": ("Create `src/main/java/com/mythik/ott/coupon/AccessCoupon.java`: a JPA entity extending `BaseEntity`. Fields:\n"
               "- `@Id @Column(name = \"id\") private String couponId;` populated in a `@PrePersist` method\n"
               "- `@ManyToOne @JoinColumn(name = \"user_id\", nullable = false) @NotNull private UserMetadata userMetadata;`\n"
               "- `@Column(name = \"code\", nullable = false) @NotNull private String code;`\n"
               "- `@Column(name = \"status\") private CouponStatus status;`\n"), "context": ""}
GOOD = {"detail": ("AccessCoupon entity (table access_coupon, extends BaseEntity like UserOwnership): non-null code, owning user, status "
                   "(CouponStatus: NOT_REDEEMED, REDEEMED), validity in days, redeemed-at; generate its id the way UserOwnership does. "
                   "Add AccessCouponRepository with a lookup by code. Schema: add the table to db-updates.sql."), "context": "UserOwnership.id() shows the id pattern."}


def test_field_by_field_specs_are_flagged_even_without_code_blocks():
    assert any("declarations" in why for why in agent.too_detailed(LIKE_THE_REAL_ONE))
    assert agent.too_detailed(GOOD) == []
    long_prose = {"detail": "word " * 400, "context": ""}
    assert any("characters long" in why for why in agent.too_detailed(long_prose))
    assert agent.too_detailed({"detail": "Call `UniqueIdUtility.generateUniqueId()` and `getCurrentUserId()` in `UserOwnershipServiceImpl`.", "context": ""}) == []   # naming things is fine


def test_job_log_lines_are_timestamped(job):
    job.log("hello")
    import re
    assert re.match(r"\d\d:\d\d:\d\d hello$", job.d["log"][-1])


def test_coder_and_manager_rules_cover_search_and_build_pitfalls():
    assert "ask" in agent.EXEC_RULES and "TASK, STEP, PLAN" in agent.EXEC_RULES
    assert "offline" in agent.PLAN_RULES and "trial and error" in agent.PLAN_RULES


# ---------- commit and push are separate approvals ----------

def test_approving_the_diff_commits_nothing_until_the_commit_is_allowed(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job); flow.execute_step(job, 1)
    flow.accept_step(job, 1)
    assert job.step(1)["status"] == "accepted" and workspace.commits(job.workspace) == []
    with pytest.raises(ValueError):
        flow.accept_step(job, 1)
    flow.approve_step(job, 1, "  Add one  ")
    assert job.step(1)["status"] == "approved" and [c["subject"] for c in workspace.commits(job.workspace)] == ["Add one"]


def test_push_and_pull_request_are_two_steps_and_a_pushed_history_is_never_squashed(job, repo, tmp_path, monkeypatch):
    from coding import flow
    monkeypatch.delenv("GITHUB_TOKEN", raising=False); monkeypatch.delenv("GH_TOKEN", raising=False)
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare)); sh(repo, "remote", "add", "origin", str(bare))
    flow.plan_turn(job); flow.freeze_plan(job)
    for n in (1, 2):
        flow.execute_step(job, n); flow.accept_step(job, n); flow.approve_step(job, n, f"step {n}")
    assert job.phase == "final" and not job.d.get("push")
    remote = flow.push_branch(job)
    assert job.d["push"] == {"done": True, "remote": str(bare), "commits": 2} and "feature/x" in subprocess.run(["git", "branch", "--list"], cwd=bare, capture_output=True, text=True).stdout
    assert job.phase == "final" and job.d["pr"] is None                          # pushed, but no pull request until it is allowed
    out = flow.open_pr(job, "T", "B")
    assert job.phase == "done" and out["opened"] is False and job.d["jira_pr"]["status"] == "unavailable"     # not GitHub: no link to attach


# ---------- blocked ----------

def test_a_blocked_step_is_reported_not_worked_around(job, monkeypatch):
    from coding import flow, jira_sync
    flow.plan_turn(job); flow.freeze_plan(job)
    answers = iter([{"status": "blocked", "summary": "stopped", "checks_run": "", "assumptions": [], "blocked_reason": "Needs the access_coupon table from task S1/T1.", "waiting_for": "S1/T1 merged"},
                    {"status": "done", "summary": "ok", "checks_run": "ok", "assumptions": []}])
    monkeypatch.setattr(agent, "execute", lambda *a, **k: {"session_id": "e", "answer": agent.clean(dict(next(answers)))})
    flow.execute_step(job, 1)
    assert job.step(1)["status"] == "blocked" and job.d["blocked"]["waiting_for"] == "S1/T1 merged" and job.current()["n"] == 1
    sent = []
    monkeypatch.setattr(jira_sync, "report_blocked", lambda task, text, client=None: sent.append(text) or {"ok": True, "note": "written"})
    flow.report_blocked(job)
    assert sent == ["Needs the access_coupon table from task S1/T1. Waiting for: S1/T1 merged."] and job.d["blocked"]["reported"]["ok"]
    flow.retry_blocked(job, 1)
    assert job.step(1)["status"] == "review" and job.d["blocked"] is None


def test_blocked_answers_are_cleaned():
    assert agent.clean({"status": "blocked", "summary": "s", "blocked_reason": "  waits  on T1 "})["blocked_reason"] == "waits on T1"
    assert agent.clean({"status": "blocked", "summary": "s"})["status"] == "done"          # no reason: it is not a block
    assert "blocked" in agent.EXEC_RULES and "blocked_reason" in agent.EXEC_RULES


# ---------- Jira when a task moves on ----------

class JiraSession:
    """Pretends to be Jira: records every call; offers In Progress / In Review / Done."""

    def __init__(self, transitions=None):
        self.calls = []
        self.transitions = transitions if transitions is not None else [
            {"id": "11", "name": "Start", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
            {"id": "21", "name": "Send to review", "to": {"name": "In Review", "statusCategory": {"key": "indeterminate"}}},
            {"id": "31", "name": "Finish", "to": {"name": "Done", "statusCategory": {"key": "done"}}}]

    def request(self, method, url, json=None, **kw):
        path = url.split("/rest/api/2")[1]
        self.calls.append((method, path, json))
        body = {"transitions": self.transitions} if path.endswith("/transitions") and method == "GET" else {}
        return type("R", (), {"status_code": 200, "headers": {}, "json": lambda s: body, "text": ""})()


def _jira_client(session):
    from tools import jira_client
    return jira_client.JiraClient("https://j.example", "P", "tok", session=session)


def test_transition_choice_prefers_review_then_progress_and_done_means_done():
    from coding import jira_sync
    ts = JiraSession().transitions
    flat = [{"id": t["id"], "name": t["name"], "to": t["to"]["name"], "category": t["to"]["statusCategory"]["key"]} for t in ts]
    assert jira_sync.pick_transition(flat, "review")["to"] == "In Review" and jira_sync.pick_transition(flat, "done")["to"] == "Done"
    no_review = [flat[0], flat[2]]
    assert jira_sync.pick_transition(no_review, "review")["to"] == "In Progress"           # any in-between state will do
    assert jira_sync.pick_transition([flat[2]], "review") is None and jira_sync.pick_transition([flat[0]], "done") is None


def test_pr_opened_attaches_the_link_and_moves_to_in_between_never_done(monkeypatch):
    from coding import jira_sync
    task = tasks.CodingTask("r", "S1", 1, "T", "d", "coding_java", jira_key="P-5")
    monkeypatch.delenv("JIRA_PR_LINK_FIELD", raising=False)
    s = JiraSession()
    out = jira_sync.after_pr(task, "https://github.com/o/r/pull/9", client=_jira_client(s))
    assert out["ok"] and "In Review" in out["note"]
    assert ("POST", "/issue/P-5/remotelink", {"object": {"url": "https://github.com/o/r/pull/9", "title": "Pull request"}}) in s.calls
    assert ("POST", "/issue/P-5/transitions", {"transition": {"id": "21"}}) in s.calls and not any(c[2] == {"transition": {"id": "31"}} for c in s.calls)
    monkeypatch.setenv("JIRA_PR_LINK_FIELD", "customfield_10042")
    s2 = JiraSession()
    assert jira_sync.after_pr(task, "https://github.com/o/r/pull/9", client=_jira_client(s2))["ok"]
    assert ("PUT", "/issue/P-5", {"fields": {"customfield_10042": "https://github.com/o/r/pull/9"}}) in s2.calls and not any("remotelink" in c[1] for c in s2.calls)
    only_done = JiraSession([JiraSession().transitions[2]])
    out = jira_sync.after_pr(task, "u", client=_jira_client(only_done))
    assert not out["ok"] and "link is attached" in out["note"]                              # linked, but there is no in-between status


def test_merged_moves_to_done_and_a_dead_end_workflow_says_so():
    from coding import jira_sync
    task = tasks.CodingTask("r", "S1", 1, "T", "d", "coding_java", jira_key="P-5")
    s = JiraSession()
    assert jira_sync.mark_done(task, client=_jira_client(s))["ok"] and ("POST", "/issue/P-5/transitions", {"transition": {"id": "31"}}) in s.calls
    out = jira_sync.mark_done(task, client=_jira_client(JiraSession([JiraSession().transitions[0]])))
    assert not out["ok"] and "In Progress" in out["note"]


def test_blocked_reason_goes_to_its_field_or_a_comment(monkeypatch):
    from coding import jira_sync
    task = tasks.CodingTask("r", "S1", 1, "T", "d", "coding_java", jira_key="P-5")
    monkeypatch.delenv("JIRA_BLOCKED_REASON_FIELD", raising=False)
    s = JiraSession()
    assert jira_sync.report_blocked(task, "needs T1", client=_jira_client(s))["ok"] and ("POST", "/issue/P-5/comment", {"body": "Blocked: needs T1"}) in s.calls
    monkeypatch.setenv("JIRA_BLOCKED_REASON_FIELD", "customfield_10050")
    s2 = JiraSession()
    jira_sync.report_blocked(task, "needs T1", client=_jira_client(s2))
    assert ("PUT", "/issue/P-5", {"fields": {"customfield_10050": "Blocked: needs T1"}}) in s2.calls


def test_pr_state_reads_github_and_merge_marks_the_job(job, monkeypatch):
    from coding import flow, jira_sync
    flow.plan_turn(job); flow.freeze_plan(job)
    job.d["pr"] = {"url": "https://github.com/o/r/pull/9", "opened": True, "note": ""}; job.d["phase"] = "done"
    monkeypatch.setattr(workspace, "pr_state", lambda url: "open")
    assert flow.check_merged(job) == "open" and not job.d.get("merged")
    monkeypatch.setattr(workspace, "pr_state", lambda url: "merged")
    monkeypatch.setattr(jira_sync, "availability", lambda task: (True, ""))
    assert flow.check_merged(job) == "merged" and job.d["merged"] and job.d["jira_done"]["status"] == "pending"
    monkeypatch.setattr(jira_sync, "mark_done", lambda task, client=None: {"ok": True, "note": "P-5 moved to Done."})
    flow.jira_mark_done(job)
    assert job.d["jira_done"] == {"status": "updated", "note": "P-5 moved to Done."}
    monkeypatch.delenv("GITHUB_TOKEN", raising=False); monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.undo()
    assert workspace.pr_state("https://github.com/o/r/pull/9") is None and workspace.pr_state("not a url") is None     # cannot tell without a token


# ---------- the task board ----------

def test_board_shows_every_task_and_how_far_its_job_got(repo, monkeypatch, tmp_path):
    from coding import board, flow
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    FakeAgent(monkeypatch)
    ts = tasks.tasks_of_run(make_run(tmp_path / "runs"))
    assert [(r.code, r.label) for r in board.rows(ts)] == [("not_started", "Not started")]
    j = flow.new_job(ts[0], "coding_python", str(repo), "", "m")
    assert board.rows(ts)[0].code == "planning"
    flow.plan_turn(j); assert board.rows(ts)[0].label == "Subtasks waiting for your approval"
    flow.freeze_plan(j); assert board.rows(ts)[0].code == "ready"
    flow.execute_step(j, 1); flow.accept_step(j, 1); flow.approve_step(j, 1, "one")
    r = board.rows(ts)[0]
    assert (r.code, r.done, r.total, r.label) == ("in_progress", 1, 2, "In progress (1 of 2 subtasks done)")
    flow.execute_step(j, 2); flow.accept_step(j, 2); flow.approve_step(j, 2, "two")
    assert board.rows(ts)[0].label == "All subtasks done; ready to push"
    j.d["phase"] = "done"; j.d["pr"] = {"url": "https://github.com/o/r/pull/1"}; j.save()
    r = board.rows(ts)[0]
    assert (r.code, r.pr_url) == ("pr_open", "https://github.com/o/r/pull/1")
    j.d["merged"] = True; j.save()
    assert board.rows(ts)[0].code == "merged"
    again = flow.new_job(ts[0], "coding_python", str(repo), "", "m")                       # a newer job takes over the row
    assert board.rows(ts)[0].job.id == again.id


# ---------- reopening: the board, then the Jira follow-ups ----------

def test_reopened_screen_shows_the_board_and_walks_the_jira_follow_ups(repo, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from coding import flow, jira_sync
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setenv("CODING_TEST_RUN", str(make_run(tmp_path / "runs")))
    FakeAgent(monkeypatch)
    task = tasks.tasks_of_run(Path(tmp_path / "runs" / "20260101-000000-aaaaaa"))[0]
    j = flow.new_job(task, "coding_python", str(repo), "", "m")                             # an earlier session got this far
    flow.plan_turn(j); flow.freeze_plan(j)
    for n in (1, 2):
        flow.execute_step(j, n); flow.accept_step(j, n); flow.approve_step(j, n, f"s{n}")
    j.d["push"] = {"done": True, "remote": "git@github.com:o/r.git", "commits": 2}
    j.d["pr"] = {"url": "https://github.com/o/r/pull/9", "opened": True, "note": ""}
    j.d["phase"] = "done"; j.d["jira_pr"] = {"status": "pending", "note": ""}; j.save()

    state = {"pr": "open"}
    monkeypatch.setattr(workspace, "pr_state", lambda url: state["pr"])
    monkeypatch.setattr(jira_sync, "availability", lambda t: (True, ""))
    done = []
    monkeypatch.setattr(jira_sync, "after_pr", lambda t, url, client=None, tests_text="": done.append("pr") or {"ok": True, "done": [], "note": "PRJ-7: link added; moved to In Review."})
    monkeypatch.setattr(jira_sync, "mark_done", lambda t, client=None: done.append("done") or {"ok": True, "note": "PRJ-7 moved to Done."})

    at = AppTest.from_function(_app, default_timeout=60).run()
    assert not at.exception, at.exception
    board_text = " ".join(e.label for e in at.expander) + " ".join(m.value for m in at.markdown)
    assert "Pull request open; waiting for merge" in board_text and "1 PR open" in board_text and "0 not started" in board_text
    at = _click(at, "Open its job")
    assert not at.exception, at.exception
    assert any("Update Jira ticket PRJ-7?" in m.value for m in at.markdown) and done == []                          # asks first
    assert any("not Done" in m.value for m in at.markdown), "it says the ticket will not be marked Done yet"
    assert not any(b.label == "Yes, mark it Done" for b in at.button), "Done is for after the merge"
    at = _click(at, "Yes, update Jira")
    assert done == ["pr"] and any("In Review" in s.value for s in at.success)
    state["pr"] = "merged"
    at = _click(at, "Check whether the pull request was merged")
    assert any("has been merged" in s.value for s in at.success)
    assert any("Move Jira ticket PRJ-7 to Done?" in m.value for m in at.markdown) and done == ["pr"]                 # asks again, for Done
    at = _click(at, "Yes, mark it Done")
    assert done == ["pr", "done"] and any("moved to Done" in s.value for s in at.success)


# ---------- unit tests: written by the coder, run by the code ----------

JUNIT = """<testsuite name="s" tests="3"><testcase classname="a.B" name="ok"/>
<testcase classname="a.B" name="bad"><failure message="expected 2 but was 3">trace</failure></testcase>
<testcase classname="a.B" name="skip"><skipped/></testcase></testsuite>"""


def test_test_commands_are_limited_to_known_runners_without_shell_tricks():
    from coding import testrun
    for ok in ("./mvnw test", "mvn -q test -Dtest=A", "python3 -m pytest -q", "python -m unittest -v", "npm test", "go test ./... -v", "./gradlew test"):
        assert testrun.validate_command(ok)[0], ok
    for bad in ("", "rm -rf /", "python3 -c 'import os'", "mvn test; rm x", "mvn test | tee x", "curl http://x", "bash run.sh"):
        assert not testrun.validate_command(bad)[0], bad


def test_results_are_read_from_junit_pytest_unittest_and_jest(tmp_path):
    from coding import testrun
    rep = tmp_path / "target" / "surefire-reports"; rep.mkdir(parents=True)
    (rep / "TEST-a.B.xml").write_text(JUNIT)
    r = testrun.read_report("BUILD FAILURE", 1, tmp_path, "mvn test", 5)
    assert (r["total"], r["passed"], r["failed"], r["skipped"]) == (3, 1, 1, 1) and r["cases"][1] == {"name": "a.B.bad", "status": "failed", "message": "expected 2 but was 3"}
    assert testrun.headline(r) == "Total 3 tests · Passed 1 · Failed 1 · Skipped 1" and not r["build_failed"]
    empty = tmp_path / "e"; empty.mkdir()
    py = testrun.read_report("FAILED tests/x.py::test_a - AssertionError: no\n=== 3 failed, 47 passed, 1 skipped in 2.1s ===\n", 1, empty, "pytest", 2)
    assert (py["total"], py["failed"], py["cases"][0]["message"]) == (51, 3, "AssertionError: no")
    ut = testrun.read_report("FAIL: test_b (t.T.test_b)\n----------\nTraceback\nAssertionError: 1 != 2\n\nRan 4 tests in 0.01s\n\nFAILED (failures=1)\n", 1, empty, "unittest", 1)
    assert (ut["total"], ut["passed"], ut["failed"]) == (4, 3, 1) and ut["cases"][0]["message"] == "AssertionError: 1 != 2"
    jest = testrun.read_report("Tests:       3 failed, 1 skipped, 47 passed, 51 total\n", 1, empty, "npm test", 1)
    assert (jest["total"], jest["failed"]) == (51, 3)


def test_a_build_that_cannot_run_here_is_reported_not_fixed(tmp_path):
    from coding import testrun
    r = testrun.read_report("[ERROR] Could not resolve dependencies for project x\n[INFO] BUILD FAILURE\n", 1, tmp_path, "./mvnw test", 3)
    assert r["ran"] is False and "could not run here" in r["note"] and not testrun.needs_fix(r)
    compile_error = testrun.read_report("[ERROR] COMPILATION ERROR :\n[ERROR] cannot find symbol\n", 1, tmp_path, "./mvnw test", 3)
    assert compile_error["ran"] and compile_error["build_failed"] and testrun.needs_fix(compile_error)         # our code does not build: that IS for the coder
    assert testrun.run("./mvnw test", tmp_path)["ran"] is False and "does not exist" in testrun.run("./mvnw test", tmp_path)["note"]
    assert testrun.run("bash x.sh", tmp_path)["ran"] is False


GOOD_CALC = "def add(a, b):\n    return a + b\n"
BAD_CALC = "def add(a, b):\n    return a - b\n"
CALC_TESTS = ("import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
              "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n    def test_negative(self):\n        self.assertEqual(add(-1, -1), -2)\n")


class Coder:
    """A scripted coder: the first turn writes buggy code with its tests; later turns follow `fixes`."""

    def __init__(self, monkeypatch, fixes):
        self.prompts, self.fixes = [], list(fixes)
        monkeypatch.setattr(agent, "execute", self.execute)

    def execute(self, agent_id, prompt, cwd, model=None, resume=None, on_event=None):
        self.prompts.append(prompt)
        cwd = Path(cwd)
        if len(self.prompts) == 1:
            (cwd / "calc.py").write_text(BAD_CALC); (cwd / "test_calc.py").write_text(CALC_TESTS)
            return {"session_id": "c1", "answer": agent.clean({"status": "done", "summary": "added add()", "tests_written": [{"file": "test_calc.py", "description": "3 cases"}],
                                                                  "test_cases": [{"name": "test_add", "verifies": "adds", "kind": "success"}]})}
        fix = self.fixes.pop(0)
        if fix.get("write"):
            (cwd / "calc.py").write_text(fix["write"])
        return {"session_id": "c1", "answer": agent.clean({"status": "done", "summary": "fixed", "unresolved": fix.get("unresolved", [])})}


def _ready_job(job, command="python3 -m unittest -v"):
    from coding import flow
    flow.plan_turn(job)
    flow.set_test_command(job, command)
    flow.freeze_plan(job)
    return flow


def test_one_round_is_one_agent_turn_and_failures_are_reported_not_fixed_automatically(job, monkeypatch):
    coder = Coder(monkeypatch, [{"write": GOOD_CALC}])
    flow = _ready_job(job)
    flow.execute_step(job, 1)
    r = job.step(1)["test_report"]
    assert len(coder.prompts) == 1 and job.step(1)["status"] == "review"       # ONE agent turn; no automatic fix rounds after it
    assert (r["total"], r["passed"], r["failed"]) == (3, 1, 2) and len(r["attempts"]) == 1 and r["unresolved"]       # the failures are just reported
    assert job.step(1)["tests_written"] == [{"file": "test_calc.py", "description": "3 cases"}]
    flow.reconsider(job, 1, "add() must add")                                    # the user starts the next round
    r = job.step(1)["test_report"]
    assert len(coder.prompts) == 2 and "add() must add" in coder.prompts[1] and (r["passed"], r["failed"]) == (3, 0)


def test_what_the_agent_could_not_fix_is_reported_with_its_own_reason(job, monkeypatch):
    class Stuck(Coder):
        def execute(self, agent_id, prompt, cwd, model=None, resume=None, on_event=None):
            out = super().execute(agent_id, prompt, cwd, model, resume, on_event)
            out["answer"]["unresolved"] = [{"test": "test_add", "reason": "The spec in the task says add() must subtract."}]
            return out
    coder = Stuck(monkeypatch, [])
    flow = _ready_job(job)
    flow.execute_step(job, 1)
    r = job.step(1)["test_report"]
    assert len(coder.prompts) == 1 and r["failed"] == 2 and r["passed"] == 1
    assert any("must subtract" in u["reason"] for u in r["unresolved"])         # the agent's own reason, for the test it named
    assert len(r["unresolved"]) == 2 and all(u["reason"] for u in r["unresolved"])       # the other gets the failure message
    assert job.step(1)["status"] == "review"                                     # the user still decides


def test_no_command_or_no_network_means_no_fix_loop(job, monkeypatch):
    coder = Coder(monkeypatch, [])
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)                                   # the fake plan has no test command
    flow.execute_step(job, 1)
    r = job.step(1)["test_report"]
    assert r["ran"] is False and "No test command" in r["note"] and len(coder.prompts) == 1
    flow.set_test_command(job, "./mvnw test")
    flow.rerun_tests(job, 1)
    assert job.step(1)["test_report"]["ran"] is False and len(coder.prompts) == 1       # ./mvnw is not in this repository: reported, nobody asked to debug it


def test_the_report_reaches_the_pull_request_and_jira(job, monkeypatch):
    from coding import jira_sync
    Coder(monkeypatch, [{"write": GOOD_CALC}])
    flow = _ready_job(job)
    flow.execute_step(job, 1); flow.reconsider(job, 1, "fix add()"); flow.accept_step(job, 1); flow.approve_step(job, 1, "one")
    md = flow.tests_markdown(job)
    assert "Step 1 - Add one: **Total 3 tests · Passed 3 · Failed 0**" in md and "`test_calc.py` - 3 cases" in md
    assert "## Unit tests" in flow.pr_description(job) and "Claude" not in flow.pr_description(job)
    text = flow.tests_text(job)
    assert text.startswith("Unit tests:") and "Passed 3" in text
    task = tasks.CodingTask("r", "S1", 1, "T", "d", "coding_java", jira_key="P-5")
    s = JiraSession()
    out = jira_sync.after_pr(task, "https://github.com/o/r/pull/9", client=_jira_client(s), tests_text=text)
    assert out["ok"] and ("POST", "/issue/P-5/comment", {"body": "Pull request: https://github.com/o/r/pull/9\n\n" + text}) in s.calls
    assert any("unit-test results" in line for line in jira_sync.planned_after_pr(task, "u", with_tests=True))


def test_screen_walks_code_then_tests_then_report_then_fix_then_commit(repo, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from coding import flow
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setenv("CODING_TEST_RUN", str(make_run(tmp_path / "runs")))
    FakeAgent(monkeypatch)
    CONFIRM_TESTS["on"] = True
    Coder(monkeypatch, [{}, {}, {}, {"unresolved": [{"test": "test_add", "reason": "the spec wants subtraction"}]}])   # 1st: the test-writing turn
    at = AppTest.from_function(_app, default_timeout=120).run()
    at.sidebar.selectbox[0].select_index(0); at.run()
    at.sidebar.text_input[0].set_value(str(repo)); at.run()
    at = _click(at, "Start a new job")
    next(t for t in at.text_input if t.label.startswith("Command that runs")).set_value("python3 -m unittest -v"); at.run()
    at = _click(at, "Freeze the subtasks")
    at = _click(at, "Execute step 1")                                                       # a. code written, shown for approval
    assert not at.exception, at.exception
    assert any("No unit test has been written or run yet" in i.value for i in at.info) and not at.metric
    assert [b.label for b in at.button if b.label.startswith("Approve")] == ["Approve the code and write the tests"]
    at = _click(at, "Approve the code and write the tests")                                 # c. tests are written, shown for approval
    assert not at.exception, at.exception
    assert any("have not been run yet" in i.value for i in at.info) and not at.metric
    at = _click(at, "Approve the tests and run them")                                       # f. run once, then the report
    assert not at.exception, at.exception
    assert {m.label: m.value for m in at.metric} == {"Total tests": "3", "Passed": "1", "Failed": "2", "Skipped": "0"}
    assert any(b.label.startswith("Let the agent fix the code") for b in at.button), "h. permission to fix is asked"
    assert not any(b.label == "Commit this step" for b in at.button)
    at = _click(at, f"Let the agent fix the code and run again (up to {flow.MAX_FIX_ATTEMPTS} rounds)")      # up to 3 rounds
    assert not at.exception, at.exception
    assert any("the spec wants subtraction" in e.value for e in at.error) and any("Run history" in c.value for c in at.caption)
    at = _click(at, "Keep the report as it is and go to the commit")                        # g. failures stay in the report
    assert any(b.label == "Commit this step" for b in at.button)
    at = _click(at, "Commit this step")
    assert not at.exception, at.exception
    pick = next(sb for sb in at.selectbox if sb.label == "Choose a subtask")                  # the records of every subtask, from a dropdown
    assert len(pick.options) == 2 and "Committed" in pick.options[0] and "Not started" in pick.options[1]
    assert [t.label for t in at.tabs] == ["Summary", "Code changes", "Unit tests", "Activity"]
    assert any("Run history" in c.value for c in at.caption), "the finished subtask's report is still readable"
    assert any("Add one" in c.value for c in at.markdown)
    pick.select_index(1); at.run()                                                            # a subtask not started yet
    assert not at.exception, at.exception
    assert any("not been started yet" in i.value for i in at.info)


# ---------- start over ----------

def test_replan_drops_the_stale_question_session_and_subtasks(job):
    from coding import flow
    job.fake.ask_first = True
    flow.plan_turn(job)
    assert job.d["question"] and job.d["plan_session"] == "p1"
    job.fake.ask_first = False
    job.d["error"] = "old error"
    flow.replan(job)
    assert job.d["question"] is None and job.d["error"] is None
    assert [s["title"] for s in job.steps] == ["Add one", "Add two"]
    assert job.fake.prompts[-1] == ("plan", None)                       # a fresh ask, not a reply to the old question


def test_replan_refuses_once_frozen(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    with pytest.raises(ValueError):
        flow.replan(job)


def test_restart_step_codes_it_again_in_a_fresh_conversation(job, monkeypatch):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    flow.execute_step(job, 1)
    job.step(1)["status"] = "asking"
    job.d["question"] = {"for": 1, "question": "q?", "options": [], "recommended_option": "", "summary": ""}
    seen = []
    orig = job.fake.execute
    monkeypatch.setattr(agent, "execute", lambda *a, **k: seen.append(k.get("resume")) or orig(*a, **k))
    flow.restart_step(job, 1)
    assert job.d["question"] is None and job.step(1)["status"] == "review"
    assert seen == [None]                                               # no resumed session: the old question is not carried over


# ---------- the approval flow: code, tests, report, fix loop, commit ----------

class Flow:
    """Drives the gated flow with a scripted coder and a scripted test runner."""

    def __init__(self, job, monkeypatch, results):
        from coding import testrun
        self.job, self.prompts, self.runs, self.results = job, [], 0, list(results)
        monkeypatch.setattr(agent, "execute", self.execute)
        monkeypatch.setattr(testrun, "run", self.run)
        job.d["confirm_tests"] = True

    def execute(self, agent_id, prompt, cwd, model=None, resume=None, on_event=None):
        self.prompts.append(prompt)
        Path(cwd, f"f{len(self.prompts)}.py").write_text("x\n")
        files = [{"file": "test_one.py", "description": "cases"}] if "write the unit tests" in prompt or "TESTS" in prompt else []
        return {"session_id": "s", "answer": agent.clean({"status": "done", "summary": "ok", "tests_written": files})}

    def run(self, command, tree, timeout=None, env=None):
        self.runs += 1
        failed = self.results.pop(0) if self.results else 0
        return {"ran": True, "command": command, "total": 3, "passed": 3 - failed, "failed": failed, "skipped": 0, "build_failed": False, "seconds": 1,
                "cases": [{"name": f"t{i}", "status": "failed", "message": f"boom {i}"} for i in range(failed)]}


def _written(job, monkeypatch, results):
    from coding import flow
    f = Flow(job, monkeypatch, results)
    flow.plan_turn(job); flow.set_test_command(job, "pytest -q"); flow.freeze_plan(job)
    flow.execute_step(job, 1)
    return flow, f


def test_a_the_code_comes_first_with_no_test_written_or_run(job, monkeypatch):
    flow, f = _written(job, monkeypatch, [])
    assert job.step(1)["status"] == "review" and f.runs == 0
    assert "Do NOT write any test file" in f.prompts[-1]
    for move in (lambda: flow.accept_step(job, 1), lambda: flow.approve_step(job, 1, "m"), lambda: flow.approve_tests(job, 1)):
        with pytest.raises(ValueError):
            move()                                                       # neither commit nor test run before the code is approved


def test_b_reconsidering_the_code_writes_it_again_from_the_users_input(job, monkeypatch):
    flow, f = _written(job, monkeypatch, [])
    flow.revise_step(job, 1, "use a smaller method")
    assert job.step(1)["status"] == "review" and "smaller method" in f.prompts[-1] and f.runs == 0


def test_c_d_approving_the_code_writes_the_tests_which_wait_to_be_approved_and_can_be_reconsidered(job, monkeypatch):
    flow, f = _written(job, monkeypatch, [])
    flow.approve_code(job, 1)
    assert job.step(1)["status"] == "testreview" and f.runs == 0 and "approved the code" in f.prompts[-1] and "Do NOT write" not in f.prompts[-1]
    flow.revise_tests(job, 1, "add an expired coupon case")                    # e. reconsider the tests
    assert job.step(1)["status"] == "testreview" and "expired coupon" in f.prompts[-1] and f.runs == 0


def test_f_g_approved_tests_run_once_and_the_report_follows_with_nothing_fixed_yet(job, monkeypatch):
    flow, f = _written(job, monkeypatch, [2])
    flow.approve_code(job, 1)
    before = len(f.prompts)
    flow.approve_tests(job, 1)
    r = job.step(1)["test_report"]
    assert job.step(1)["status"] == "results" and f.runs == 1 and len(f.prompts) == before     # failures: no fix without permission
    assert (r["passed"], r["failed"]) == (1, 2) and [u["reason"] for u in r["unresolved"]] == ["boom 0", "boom 1"]
    with pytest.raises(ValueError):
        flow.approve_step(job, 1, "m")                                         # no commit straight from the report


def test_h_i_fixing_needs_permission_uses_the_users_input_and_stops_when_everything_passes(job, monkeypatch):
    flow, f = _written(job, monkeypatch, [2, 1, 0])
    flow.approve_code(job, 1); flow.approve_tests(job, 1)
    flow.fix_and_rerun(job, 1, "the expiry counts from the redeem time")
    assert "expiry counts from the redeem time" in f.prompts[-2]               # the user's words reach the coder (first round)
    assert f.runs == 3 and job.step(1)["status"] == "results" and job.step(1)["test_report"]["failed"] == 0
    assert [a["run"] for a in job.step(1)["test_report"]["attempts"]] == [1, 2, 3]
    with pytest.raises(ValueError):
        flow.fix_and_rerun(job, 1)                                              # nothing left to fix


def test_j_k_l_the_fix_loop_stops_after_three_rounds_and_a_new_request_gives_three_more(job, monkeypatch):
    flow, f = _written(job, monkeypatch, [2] * 20)
    flow.approve_code(job, 1); flow.approve_tests(job, 1)
    fixes = lambda: sum("unit tests were run" in p for p in f.prompts)
    flow.fix_and_rerun(job, 1)
    assert fixes() == flow.MAX_FIX_ATTEMPTS == 3 and f.runs == 4               # 3 rounds, then the failures stay in the report
    assert job.step(1)["status"] == "results" and job.step(1)["test_report"]["failed"] == 2
    flow.fix_and_rerun(job, 1, "try the other approach")                       # the user asks again
    assert fixes() == 6 and f.runs == 7
    assert [a["run"] for a in job.step(1)["test_report"]["attempts"]] == list(range(1, 8))


def test_m_n_the_commit_is_the_last_decision_and_can_wait(job, monkeypatch, repo):
    flow, f = _written(job, monkeypatch, [2])
    flow.approve_code(job, 1); flow.approve_tests(job, 1)
    flow.accept_results(job, 1)                                                # keep the failing report as it is
    assert job.step(1)["status"] == "accepted" and workspace.commits(job.workspace) == []       # nothing committed until allowed
    flow.approve_step(job, 1, "Add one")
    assert job.step(1)["status"] == "approved" and [c["subject"] for c in workspace.commits(job.workspace)] == ["Add one"]


def test_the_tests_can_be_left_unrun_and_the_report_says_so(job, monkeypatch):
    flow, f = _written(job, monkeypatch, [])
    flow.approve_code(job, 1); flow.skip_running_tests(job, 1)
    r = job.step(1)["test_report"]
    assert job.step(1)["status"] == "results" and r["ran"] is False and "not to run" in r["note"] and f.runs == 0


def test_a_missing_test_command_is_found_from_the_build_files(job, monkeypatch):
    flow, f = _written(job, monkeypatch, [])
    job.d["test_command"] = ""
    Path(job.workspace.tree, "pom.xml").write_text("")
    flow.approve_code(job, 1); flow.approve_tests(job, 1)
    assert job.step(1)["test_report"]["command"] == "mvn test"


# ---------- older sessions get the same flow ----------

def _saved(job, status, **extra):
    job.d["confirm_tests"] = True
    job.d["plan"]["steps"] = [{**job.step(1), "status": status, **extra}] + job.d["plan"]["steps"][1:]
    job.save()
    from coding import flow
    return flow.load(job.id)


def test_old_sessions_are_moved_back_to_the_code_approval_when_loaded(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    for status, extra in (("caseplan", {}), ("testreview", {"phase": "code"}), ("testreview", {}), ("accepted", {})):
        loaded = _saved(job, status, **extra)
        assert loaded.step(1)["status"] == "review" and loaded.step(1)["phase"] == "code", (status, extra)


def test_loading_leaves_new_flow_steps_and_committed_steps_alone(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    assert _saved(job, "testreview", phase="tests").step(1)["status"] == "testreview"          # written the new way
    assert _saved(job, "accepted", tests_done=True).step(1)["status"] == "accepted"            # went through the report
    assert _saved(job, "approved").step(1)["status"] == "approved"                              # committed
    assert _saved(job, "results").step(1)["status"] == "results"
    job.d["confirm_tests"] = False                                                              # a job that never used the gates
    job.d["plan"]["steps"][0]["status"] = "accepted"; job.save()
    assert flow.load(job.id).step(1)["status"] == "accepted"


# ---------- Jira hears about each committed subtask, and about a branch with no pull request ----------

def _stepdict():
    return {"n": 2, "title": "Coupon service", "commit": "abcdef1234567", "summary": "Added redeem().", "tests_written": [{"file": "T.java", "description": ""}],
            "test_report": {"ran": True, "command": "./mvnw test", "total": 3, "passed": 2, "failed": 1, "skipped": 0, "build_failed": False,
                            "unresolved": [{"test": "testExpiry", "reason": "expects UTC"}]}}


def test_a_committed_subtask_is_posted_to_jira_as_a_comment_with_its_report():
    from coding import jira_sync
    task = tasks.CodingTask("r", "S1", 1, "T", "d", "coding_java", jira_key="P-5")
    s = JiraSession()
    out = jira_sync.report_step(task, _stepdict(), "feature/x", client=_jira_client(s))
    assert out["ok"] and "Subtask 2" in out["note"]
    method, path, body = next(c for c in s.calls if c[1] == "/issue/P-5/comment")
    text = body["body"]
    assert method == "POST" and "Coupon service" in text and "abcdef12" in text and "feature/x" in text
    assert "NOT FIXED: testExpiry" in text and "expects UTC" in text and "T.java" in text


def test_no_pull_request_still_lets_the_branch_and_results_reach_jira_without_moving_the_ticket():
    from coding import jira_sync
    task = tasks.CodingTask("r", "S1", 1, "T", "d", "coding_java", jira_key="P-5")
    s = JiraSession()
    out = jira_sync.after_branch(task, "feature/x", "https://github.com/o/r/compare/feature/x", client=_jira_client(s), tests_text="Unit tests: all pass")
    assert out["ok"] and any(c[1] == "/issue/P-5/comment" and "feature/x" in c[2]["body"] and "all pass" in c[2]["body"] for c in s.calls)
    assert not any("transitions" in c[1] and c[0] == "POST" for c in s.calls)                 # the status is left alone


def test_committing_posts_to_jira_only_when_the_box_was_ticked_and_a_jira_error_never_blocks_the_commit(job, monkeypatch):
    from coding import flow, jira_sync
    posted = []
    monkeypatch.setattr(jira_sync, "report_step", lambda task, step, branch: posted.append(step["n"]) or {"ok": False, "note": "Jira said no"})
    flow.plan_turn(job); flow.freeze_plan(job)
    flow.execute_step(job, 1); flow.approve_step(job, 1, "one")
    assert posted == [] and job.step(1)["status"] == "approved" and "jira_note" not in job.step(1)          # not ticked: Jira not told
    flow.execute_step(job, 2); flow.approve_step(job, 2, "two", post_to_jira=True)
    assert posted == [2] and job.step(2)["status"] == "approved" and job.step(2)["jira_note"] == "Jira said no"   # committed anyway


# ---------- the coder has a shell, and what it runs is recorded ----------

MAVEN_OUT = ("[INFO] Running com.x.CouponServiceTest\n[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.1 s <<< FAILURE!\n"
             "[ERROR] Failures: \n[ERROR]   CouponServiceTest.testExpiry:41 expected: <30> but was: <29>\n[INFO] \n"
             "[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0\n[INFO] BUILD FAILURE\n")


def test_test_commands_are_told_apart_from_other_shell_commands():
    from coding import testrun
    yes = ["./mvnw test", "./mvnw -q test -Dtest=CouponServiceTest", "mvn verify", "python3 -m pytest -q", "pytest tests/", "npm test", "./gradlew test", "go test ./...",
           "cd x && ./mvnw test"]
    no = ["./mvnw -q compile", "./mvnw test-compile", "./mvnw package -DskipTests", "ls -la", "git diff", "cat pom.xml", "grep -n test pom.xml", "mkdir -p src/test"]
    assert all(testrun.is_test_command(c) for c in yes), [c for c in yes if not testrun.is_test_command(c)]
    assert not any(testrun.is_test_command(c) for c in no), [c for c in no if testrun.is_test_command(c)]


def test_an_agent_test_run_is_read_from_its_own_output():
    from coding import testrun
    r = testrun.read_agent_run("./mvnw test", MAVEN_OUT, True, "10:01:02", "fix")
    assert (r["total"], r["passed"], r["failed"], r["ran"]) == (3, 2, 1, True) and r["phase"] == "fix" and r["when"] == "10:01:02"
    assert r["cases"] and "CouponServiceTest.testExpiry" in r["cases"][0]["name"] and "expected" in r["cases"][0]["message"]
    assert testrun.run_headline(r) == "2 passed, 1 failed of 3"
    broken = testrun.read_agent_run("./mvnw test", "[ERROR] COMPILATION ERROR\n", True)
    assert broken["ran"] is False and testrun.run_headline(broken) == "the build failed, no test ran" and broken["status"] == "build_failed"


def test_the_sdk_stream_gives_every_bash_call_and_its_result(monkeypatch):
    import asyncio
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

    async def fake_query(prompt, options):
        assert "Bash" in options.allowed_tools and options.env.get("BASH_MAX_TIMEOUT_MS")
        yield AssistantMessage(content=[ToolUseBlock(id="a", name="Bash", input={"command": "./mvnw test"}),
                                        ToolUseBlock(id="b", name="Read", input={"file_path": "x"})], model="m")
        yield UserMessage(content=[ToolResultBlock(tool_use_id="a", content=[{"type": "text", "text": MAVEN_OUT}], is_error=True),
                                   ToolResultBlock(tool_use_id="b", content="file text")])
        yield AssistantMessage(content=[TextBlock(text="done")], model="m")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s1",
                            structured_output={"status": "done", "summary": "ok"})
    monkeypatch.setattr(agent, "query", fake_query)
    out = asyncio.run(agent._run("p", "sys", "/tmp", None, None, None, agent.EXEC_SCHEMA, agent.ALLOWED, agent.DISALLOWED, 5))
    assert [(r["command"], r["is_error"]) for r in out["bash_runs"]] == [("./mvnw test", True)]      # only the shell call, with its result
    assert "Tests run: 3" in out["bash_runs"][0]["output"] and out["session_id"] == "s1"
    monkeypatch.setattr(agent, "query", lambda prompt, options: _no_answer())
    with pytest.raises(agent.Incomplete) as e:
        asyncio.run(agent._run("p", "sys", "/tmp", None, None, None, agent.EXEC_SCHEMA, agent.ALLOWED, agent.DISALLOWED, 5))
    assert e.value.bash_runs == []                                                                  # kept on the error, so a failed turn still records


async def _no_answer():
    from claude_agent_sdk import ResultMessage
    yield ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=80, session_id="s2")


def test_what_the_agents_shell_ran_is_saved_with_the_step_and_shown(job, monkeypatch):
    from coding import flow, testrun
    job.d["confirm_tests"] = True
    flow.plan_turn(job); flow.set_test_command(job, "pytest -q"); flow.freeze_plan(job)
    real = job.fake.execute
    runs = [{"command": "./mvnw -q compile", "output": "ok", "is_error": False, "when": "10:00:01"},
            {"command": "./mvnw test", "output": MAVEN_OUT, "is_error": True, "when": "10:00:09"}]
    monkeypatch.setattr(agent, "execute", lambda *a, **k: {**real(*a, **k), "bash_runs": runs})
    flow.execute_step(job, 1)
    step = job.step(1)
    assert [c["command"] for c in step["agent_commands"]] == ["./mvnw -q compile"]                   # other shell commands: a short line
    assert [(r["command"], r["failed"], r["phase"]) for r in step["agent_runs"]] == [("./mvnw test", 1, "code")]
    assert flow.load(job.id).step(1)["agent_runs"][0]["tail"]                                        # saved with the job


# ---------- autonomous mode: the agent does it all, one review at the end ----------

class Auto:
    """A scripted coder for autonomous mode: one turn writes code and tests, and reports the command that worked."""

    def __init__(self, monkeypatch, command="python3 -m unittest -v"):
        self.prompts, self.command = [], command
        monkeypatch.setattr(agent, "execute", self.execute)

    def execute(self, agent_id, prompt, cwd, model=None, resume=None, on_event=None):
        self.prompts.append((prompt, resume))
        Path(cwd, "calc.py").write_text(GOOD_CALC); Path(cwd, "test_calc.py").write_text(CALC_TESTS)
        return {"session_id": "a1", "bash_runs": [{"command": "python3 -m unittest -v", "output": "Ran 3 tests in 0.1s\n\nOK\n", "is_error": False, "when": "10:00:00"}],
                "answer": agent.clean({"status": "done", "summary": "did it all", "test_command": self.command,
                                       "tests_written": [{"file": "test_calc.py", "description": "3 cases"}]})}


def test_autonomous_is_the_default_and_one_turn_does_everything_then_one_review(job, monkeypatch):
    from coding import flow
    a = Auto(monkeypatch)
    assert not flow.steps_mode(job) and "confirm_tests" in job.d and job.d["confirm_tests"] is False
    flow.plan_turn(job); flow.freeze_plan(job)
    flow.execute_step(job, 1)
    s = job.step(1)
    assert s["status"] == "review" and len(a.prompts) == 1 and "do the whole subtask yourself" in a.prompts[0][0]    # one agent turn, no gates
    assert "Do NOT write any test file" not in a.prompts[0][0]
    assert s["test_report"]["ran"] and s["test_report"]["passed"] == 3 and s["test_report"]["failed"] == 0         # the program's official run
    assert [r["command"] for r in s["agent_runs"]] == ["python3 -m unittest -v"]                                    # and what the agent ran itself
    flow.accept_step(job, 1)                                                                                         # approve the changes ...
    assert s["status"] == "accepted" and workspace.commits(job.workspace) == []
    flow.approve_step(job, 1, "Add one")                                                                             # ... then allow the commit
    assert s["status"] == "approved" and len(workspace.commits(job.workspace)) == 1


def test_the_command_the_agent_reports_runs_the_official_tests_unless_the_user_set_one(job, monkeypatch):
    from coding import flow, testrun
    seen = []
    monkeypatch.setattr(testrun, "run", lambda cmd, tree, timeout=None, env=None: seen.append(cmd) or {"ran": True, "command": cmd, "total": 1, "passed": 1, "failed": 0,
                        "skipped": 0, "cases": [], "build_failed": False, "seconds": 1})
    Auto(monkeypatch, command=".venv/bin/python -m pytest -q")
    flow.plan_turn(job); flow.freeze_plan(job)
    flow.execute_step(job, 1)
    assert seen == [".venv/bin/python -m pytest -q"] and job.step(1)["test_command"] == ".venv/bin/python -m pytest -q"     # the venv's own interpreter
    flow.set_test_command(job, "pytest -x")
    assert flow.step_command(job, job.step(1)) == "pytest -x"                                                          # the user's word wins


def test_reconsidering_continues_the_same_session_and_keeps_the_work(job, monkeypatch):
    from coding import flow
    a = Auto(monkeypatch)
    flow.plan_turn(job); flow.set_test_command(job, "python3 -m unittest -v"); flow.freeze_plan(job)
    flow.execute_step(job, 1)
    flow.reconsider(job, 1, "also handle negative numbers")
    prompt, resume = a.prompts[-1]
    assert resume == "a1" and "also handle negative numbers" in prompt and "still in the working tree" in prompt      # same session, nothing wiped
    assert Path(job.workspace.tree, "calc.py").exists() and job.step(1)["status"] == "review"                          # back at the one review
    assert len(workspace.commits(job.workspace)) == 0


def test_step_by_step_is_still_there_when_asked_for(repo, tmp_path, monkeypatch):
    from coding import flow
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    CONFIRM_TESTS["on"] = False
    task = tasks.tasks_of_run(make_run(tmp_path / "runs"))[0]
    j = flow.new_job(task, "coding_python", str(repo), "", "m")
    k = flow.new_job(task, "coding_python", str(repo), "", "m", step_by_step=True)
    assert not flow.steps_mode(j) and flow.steps_mode(k) and k.d["confirm_tests"] is True


def test_old_saved_sessions_open_in_autonomous_mode_with_one_review(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    del job.d["confirm_tests"]                                    # a job saved before the mode existed
    for status in ("testreview", "results", "caseplan"):
        job.d["plan"]["steps"][0]["status"] = status; job.save()
        loaded = flow.load(job.id)
        assert not flow.steps_mode(loaded) and loaded.step(1)["status"] == "review", status
    job.d["plan"]["steps"][0]["status"] = "accepted"; job.save()
    assert flow.load(job.id).step(1)["status"] == "accepted"      # the user already approved this one: left as it is


# ---------- redo and revamp finished work ----------

def _two_commits(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    for n in (1, 2):
        flow.execute_step(job, n); flow.approve_step(job, n, f"step {n}")
    assert job.phase == "final" and len(workspace.commits(job.workspace)) == 2


def test_redo_from_a_step_rewrites_only_the_agents_branch_and_keeps_a_backup(job):
    from coding import flow
    _two_commits(job)
    old_tip = workspace.head(job.workspace)
    backup = flow.redo_from(job, 2, "use a different approach")
    assert [c["subject"] for c in workspace.commits(job.workspace)] == ["step 1"]                          # step 2's commit is gone from the branch
    assert job.step(1)["status"] == "approved" and job.step(2)["status"] == "pending" and job.step(2)["commit"] is None
    assert job.step(2)["redo_note"] == "use a different approach" and job.phase == "executing" and job.d["exec_sessions"] == {}
    assert workspace.git(job.workspace.tree, "rev-parse", backup) == old_tip                                 # the old work can still be got back
    assert job.d["history"][-1]["from_step"] == 2 and len(job.d["history"][-1]["commits"]) == 2
    flow.execute_step(job, 2)
    assert "BEING REDONE" in job.fake.prompts[-1][1] and "use a different approach" in job.fake.prompts[-1][1]


def test_a_merged_pull_request_or_an_unstarted_step_cannot_be_redone(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    with pytest.raises(ValueError):
        flow.redo_from(job, 1)                                    # never started
    flow.execute_step(job, 1); flow.approve_step(job, 1, "one")
    job.d["merged"] = True
    with pytest.raises(ValueError) as e:
        flow.redo_from(job, 1)
    assert "merged" in str(e.value) and [c["subject"] for c in workspace.commits(job.workspace)] == ["one"]  # nothing was touched


def test_pushing_a_redone_branch_needs_confirmation_and_then_replaces_the_remote(job, repo, tmp_path):
    from coding import flow
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare)); sh(repo, "remote", "add", "origin", str(bare))
    _two_commits(job)
    flow.push_branch(job)
    first_remote_tip = workspace.git(bare, "rev-parse", job.workspace.branch)
    flow.redo_from(job, 2, "again")
    assert job.d["push"]["stale"] is True and workspace.git(bare, "rev-parse", job.workspace.branch) == first_remote_tip   # redoing touched nothing remote
    flow.execute_step(job, 2); flow.approve_step(job, 2, "step 2 again")
    with pytest.raises(workspace.GitError):
        flow.push_branch(job)                                                                                 # no silent overwrite
    assert workspace.git(bare, "rev-parse", job.workspace.branch) == first_remote_tip
    flow.push_branch(job, replace_remote=True)
    assert workspace.git(bare, "rev-parse", job.workspace.branch) == workspace.head(job.workspace) != first_remote_tip   # now it was replaced
    assert job.d["repushed"] is True and not job.d["push"].get("stale")


def test_revamp_starts_a_new_job_from_the_plan_on_a_fresh_branch_and_leaves_the_old_one(job, repo, tmp_path):
    from coding import flow
    _two_commits(job)
    job.d["merged"] = True
    new = flow.revamp_job(job, "split the service")
    assert new.id != job.id and new.d["revamp_of"] == job.id and new.workspace.branch != job.workspace.branch
    assert new.phase == "executing" and new.d["plan"]["frozen"] and [s["status"] for s in new.steps] == ["pending", "pending"]
    assert all(s["redo_note"] == "split the service" and s["commit"] is None for s in new.steps)
    assert [s["status"] for s in job.steps] == ["approved", "approved"] and job.d["history"][-1]["revamped_as"] == new.id    # the old record is kept


# ---------- the agent's test runs lead the report: status, every test, why each failure failed ----------

SUREFIRE = ('<testsuite name="CouponServiceTest" tests="3"><testcase classname="CouponServiceTest" name="redeemOk"/>'
            '<testcase classname="CouponServiceTest" name="redeemTwice"/>'
            '<testcase classname="CouponServiceTest" name="expiry"><failure message="expected: &lt;30&gt; but was: &lt;29&gt;">stack</failure></testcase></testsuite>')


def test_a_quiet_build_is_read_from_the_test_report_files_with_every_test_and_the_reason(tmp_path):
    import os, time
    from coding import testrun
    d = tmp_path / "target" / "surefire-reports"
    d.mkdir(parents=True)
    (d / "TEST-CouponServiceTest.xml").write_text(SUREFIRE)
    stale = d / "TEST-Old.xml"
    stale.write_text(SUREFIRE.replace("CouponServiceTest", "OldTest"))
    os.utime(stale, (time.time() - 7200, time.time() - 7200))                                  # left by an earlier day: not this run
    r = testrun.read_agent_run("JAVA_HOME=/jdk ./mvnw -q test", "{\"json\": \"log noise\"}", False, "10:00:00", "code", tmp_path, time.time())
    assert (r["total"], r["passed"], r["failed"], r["status"], r["source"]) == (3, 2, 1, "failed", "test report files")
    assert {c["name"]: c["status"] for c in r["cases"]} == {"CouponServiceTest.redeemOk": "passed", "CouponServiceTest.redeemTwice": "passed", "CouponServiceTest.expiry": "failed"}
    assert "expected: <30> but was: <29>" in next(c["message"] for c in r["cases"] if c["status"] == "failed")
    assert testrun.run_headline(r) == "2 passed, 1 failed of 3"


def test_a_run_that_never_reached_the_tests_says_why():
    from coding import testrun
    r = testrun.read_agent_run("./mvnw test", "[ERROR] Fatal error compiling: error: release version 21 not supported -> [Help 1]\n", True)
    assert r["status"] == "build_failed" and not r["ran"] and "too old" in r["reason"] and "JAVA_HOME" in r["reason"]
    c = testrun.read_agent_run("./mvnw test", "[ERROR] COMPILATION ERROR :\n[ERROR] /x/A.java:[3,1] cannot find symbol\n", True)
    assert c["status"] == "build_failed" and "cannot find symbol" in c["reason"]


def test_screen_leads_with_the_agents_own_test_runs_and_their_failure_reasons(repo, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from coding import flow
    monkeypatch.setattr(flow, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setenv("CODING_TEST_RUN", str(make_run(tmp_path / "runs")))
    FakeAgent(monkeypatch)
    agent_said = ("test_a (t.T.test_a) ... ok\ntest_b (t.T.test_b) ... FAIL\ntest_c (t.T.test_c) ... FAIL\n\n======\nFAIL: test_b (t.T.test_b)\n------\n"
                  "Traceback (most recent call last):\nAssertionError: 1 != 2\n\nFAIL: test_c (t.T.test_c)\n------\nAssertionError: 3 != 4\n\n"
                  "Ran 5 tests in 0.1s\n\nFAILED (failures=2)\n")

    class Agent(Auto):
        def execute(self, *a, **k):
            out = super().execute(*a, **k)
            out["bash_runs"] = [{"command": "python3 -m unittest -v", "output": agent_said, "is_error": True, "when": "10:00:00", "ts": 0}]
            return out
    Agent(monkeypatch)
    at = AppTest.from_function(_app, default_timeout=120).run()
    at.sidebar.selectbox[0].select_index(0); at.run()
    at.sidebar.text_input[0].set_value(str(repo)); at.run()
    at = _click(at, "Start a new job")
    at = _click(at, "Freeze the subtasks")
    at = _click(at, "Execute step 1")
    assert not at.exception, at.exception
    assert {m.label: m.value for m in at.metric} == {"Total tests": "5", "Passed": "3", "Failed": "2", "Skipped": "0"}   # the AGENT's run, not the program's 3/3/0
    assert any("Result of the agent's last test run" in m.value for m in at.markdown)
    assert any("Independent re-run by the program" in m.value for m in at.markdown)
    assert any("test_b" in e.value and "1 != 2" in e.value for e in at.error)                                           # why each failure failed
    assert any("test_calc.py" in x.label for x in at.expander), [x.label for x in at.expander]                         # the unit test code itself, as a dropdown
    assert any("class T(unittest.TestCase)" in c.value and "def test_add" in c.value for c in at.code)


def test_every_place_reports_the_same_result_for_a_subtask(job, monkeypatch):
    from coding import flow, jira_sync, testrun
    flow.plan_turn(job); flow.freeze_plan(job)
    flow.execute_step(job, 1)
    s = job.step(1)
    s["agent_runs"] = [{"when": "10:00:00", "phase": "code", "command": "JAVA_HOME=/jdk ./mvnw test", "ok": True, "ran": True, "status": "passed",
                        "passed": 3, "failed": 0, "skipped": 0, "total": 3, "cases": [], "tail": ""}]
    s["test_report"] = {"ran": True, "build_failed": True, "total": 0, "passed": 0, "failed": 0, "skipped": 0, "cases": [], "command": "./mvnw test",
                        "note": "The build failed before any test ran.", "attempts": [], "unresolved": [{"test": "(the build)", "reason": "jdk"}]}
    r = testrun.step_result(s)
    assert r["headline"].startswith("Total 3 tests · Passed 3 · Failed 0") and r["unresolved"] == [] and "re-run" in r["rerun"] and "build failed" in r["rerun"]
    assert flow.tests_overview(job)[0]["headline"] == r["headline"]                                                 # the final screen
    assert "Passed 3" in flow.tests_text(job) and "(the build)" not in flow.tests_text(job)                          # Jira and the PR
    assert "Passed 3" in flow.tests_markdown(job) and "(the build)" not in flow.tests_markdown(job)
    assert "Passed 3" in jira_sync.step_comment(s, "feature/x")                                                    # the per-subtask Jira comment
    s["agent_runs"] = []                                                                                           # no agent run with results: the program's stands in
    assert testrun.step_result(s)["source"] == "program" and "build failed" in testrun.step_result(s)["headline"]


def test_test_files_are_found_by_convention_even_if_the_agent_did_not_list_them(job):
    from coding import flow, testrun
    for yes in ("src/test/java/com/x/CouponServiceTest.java", "tests/test_calc.py", "test_calc.py", "web/src/App.test.tsx", "pkg/calc_test.go", "Sources/AppTests.swift"):
        assert testrun.is_test_file(yes), yes
    for no in ("src/main/java/com/x/CouponService.java", "calc.py", "README.md", "src/main/resources/application.properties"):
        assert not testrun.is_test_file(no), no
    flow.plan_turn(job); flow.freeze_plan(job)
    flow.execute_step(job, 1)
    Path(job.workspace.tree, "test_extra.py").write_text("def test_x():\n    assert True\n")
    assert flow.test_file_paths(job, job.step(1)) == ["test_extra.py"]                    # found from the step's own changes
    flow.approve_step(job, 1, "one")
    assert flow.test_file_paths(job, job.step(1)) == ["test_extra.py"]                    # and from its own commit afterwards


def test_the_job_header_is_a_card_with_the_mode_and_one_segment_per_subtask(job):
    from coding import ui, flow
    import html as h
    shown = []
    import streamlit as st
    orig = st.markdown
    try:
        st.markdown = lambda body, **kw: shown.append(body)
        flow.plan_turn(job); flow.freeze_plan(job)
        ui._hero(job)
    finally:
        st.markdown = orig
    card = shown[-1]
    assert "sd-hero" in card and card.count("<span class='sd-seg ") == 2 and "Autonomous" in card and "0 of 2 subtasks done" in card
    assert h.escape(job.task.label) in card and job.workspace.branch in card
    job.d["confirm_tests"] = True
    shown.clear()
    try:
        st.markdown = lambda body, **kw: shown.append(body)
        ui._hero(job)
    finally:
        st.markdown = orig
    assert "Step by step" in shown[-1]


# ---------- one undo per task ----------

def test_undo_the_plan_drops_the_subtasks_without_asking_the_manager_again(job):
    from coding import flow
    flow.plan_turn(job)
    asked = len(job.fake.prompts)
    flow.discard_plan(job)
    assert job.steps == [] and job.phase == "planning" and job.d["plan_session"] is None
    assert len(job.fake.prompts) == asked                                           # nothing was re-planned
    flow.plan_turn(job); flow.freeze_plan(job)
    with pytest.raises(ValueError):
        flow.discard_plan(job)                                                      # a frozen plan is undone by undoing the freeze


def test_undo_the_freeze_only_until_a_step_starts_or_jira_is_told(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    assert flow.unfreeze_blocker(job) == ""
    job.d["jira"] = {"status": "updated", "note": "x"}
    assert "Jira" in flow.unfreeze_blocker(job)
    job.d["jira"] = None
    flow.execute_step(job, 1)
    assert "started" in flow.unfreeze_blocker(job)


def test_undo_a_step_in_review_removes_its_edits_and_nothing_else(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    flow.execute_step(job, 1); flow.approve_step(job, 1, "one")
    flow.execute_step(job, 2)
    assert (job.workspace.tree / "two.py").exists()
    flow.undo_step(job, 2)
    assert not (job.workspace.tree / "two.py").exists() and job.step(2)["status"] == "pending" and job.step(2)["start_sha"] is None
    assert job.step(1)["status"] == "approved" and [c["subject"] for c in workspace.commits(job.workspace)] == ["one"]
    assert len([p for p in job.fake.prompts if p[0] == "exec"]) == 2                 # the agent was not run again


def test_undo_a_commit_only_from_the_newest_step_backwards(job):
    from coding import flow
    _two_commits(job)
    assert "Undo step 2 first" in flow.undo_step_blocker(job, 1)
    with pytest.raises(ValueError):
        flow.undo_step(job, 1)
    old_tip = workspace.head(job.workspace)
    flow.undo_step(job, 2)
    assert job.step(2)["status"] == "pending" and job.step(1)["status"] == "approved" and job.phase == "executing"
    assert [c["subject"] for c in workspace.commits(job.workspace)] == ["step 1"]
    assert job.d["history"][-1]["backup"] and workspace.git(job.workspace.tree, "rev-parse", job.d["history"][-1]["backup"]) == old_tip
    flow.undo_step(job, 1)
    assert workspace.commits(job.workspace) == []


def test_undo_skipping_a_step_and_a_merged_pr_blocks_every_commit_undo(job):
    from coding import flow
    flow.plan_turn(job); flow.freeze_plan(job)
    flow.skip_step(job, 1)
    flow.execute_step(job, 2); flow.approve_step(job, 2, "two")
    assert job.phase == "final"
    assert flow.undo_step_blocker(job, 1) == ""                       # a skip has no commit under later work, so it can be undone alone
    flow.undo_step(job, 1)
    assert job.step(1)["status"] == "pending" and job.phase == "executing" and job.step(2)["status"] == "approved"
    job.d["merged"] = True
    assert "merged" in flow.undo_step_blocker(job, 2)


def test_undo_the_push_deletes_only_the_remote_branch_and_not_while_a_pr_is_open(job, repo, tmp_path):
    from coding import flow
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare)); sh(repo, "remote", "add", "origin", str(bare))
    assert "Nothing has been pushed" in flow.undo_push_blocker(job)
    _two_commits(job)
    flow.push_branch(job)
    branch = job.workspace.branch
    job.d["pr"] = {"url": "https://github.com/o/r/pull/1", "opened": True}
    assert "close it first" in flow.undo_push_blocker(job)
    job.d["pr"] = None
    flow.undo_push(job)
    assert branch not in workspace.git(bare, "branch", "--list") and job.d["push"] is None
    assert [c["subject"] for c in workspace.commits(job.workspace)] == ["step 1", "step 2"]          # the work is still here
    flow.push_branch(job)                                                                          # and can be pushed again
    assert branch in workspace.git(bare, "branch", "--list")


def test_undo_the_pull_request_closes_it_and_keeps_the_branch(job, monkeypatch):
    from coding import flow
    _two_commits(job)
    job.d.update(pr={"url": "https://github.com/o/r/pull/9", "opened": True}, phase="done", push={"done": True, "commits": 2, "remote": "x"})
    closed = []
    monkeypatch.setattr(workspace, "close_pr", lambda url: closed.append(url))
    flow.undo_pr(job)
    assert closed == ["https://github.com/o/r/pull/9"] and job.d["pr"] is None and job.phase == "final" and job.d["push"]["done"]
    assert "No pull request" in flow.undo_pr_blocker(job)
    job.d.update(pr={"url": "u", "opened": True}, merged=True)
    assert "merged" in flow.undo_pr_blocker(job)


def test_close_pr_needs_a_github_address_and_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False); monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(workspace.GitError, match="not on GitHub"):
        workspace.close_pr("https://example.com/x/1")
    with pytest.raises(workspace.GitError, match="GITHUB_TOKEN"):
        workspace.close_pr("https://github.com/o/r/pull/1")


# ---------- git never waits for a password in the terminal ----------

def test_network_git_never_prompts_and_only_github_https_gets_the_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sekret")
    env = workspace.network_env("https://github.com/o/r.git")
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_COUNT"] == "1" and "sekret" not in env["GIT_CONFIG_VALUE_0"]
    import base64
    assert base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == "x-access-token:sekret"     # sent as GitHub expects, in memory only
    other = workspace.network_env("https://gitlab.com/o/r.git")
    assert other["GIT_TERMINAL_PROMPT"] == "0" and "GIT_CONFIG_VALUE_0" not in other                      # the token goes to GitHub only
    monkeypatch.delenv("GITHUB_TOKEN"); monkeypatch.delenv("GH_TOKEN", raising=False)
    assert "GIT_CONFIG_VALUE_0" not in workspace.network_env("https://github.com/o/r.git")


def test_only_the_commands_that_reach_a_remote_get_the_no_prompt_environment(monkeypatch, tmp_path):
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd[1], (kw.get("env") or {}).get("GIT_TERMINAL_PROMPT")))
        return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/o/r.git\n" if cmd[1:3] == ["remote", "get-url"] else "", stderr="")
    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    for command in ("fetch", "push", "status", "log"):
        workspace.git(tmp_path, command)
    assert [(c, p) for c, p in seen if c in ("fetch", "push", "status", "log")] == [("fetch", "0"), ("push", "0"), ("status", None), ("log", None)]


def test_a_missing_login_becomes_advice_not_a_hang(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: could not read Username for 'https://github.com': terminal prompts disabled\n")
    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    with pytest.raises(workspace.GitError) as err:
        workspace.git(tmp_path, "fetch", "origin", "feature/x")
    assert "GITHUB_TOKEN" in str(err.value) and "terminal prompts disabled" not in str(err.value)
    assert workspace.explain("fatal: something else") == "fatal: something else"


def test_ordinary_git_against_a_local_remote_is_unaffected(repo, tmp_path):
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare)); sh(repo, "remote", "add", "origin", str(bare))
    workspace.git(repo, "push", "origin", "HEAD:refs/heads/side")
    workspace.git(repo, "fetch", "origin")
    assert "side" in workspace.git(bare, "branch", "--list")
