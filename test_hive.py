"""Plain-assert checks for the hive, the optional knowledge base, the
Design / Release agents' validation, and a whole from-scratch pipeline run.
Run: python3 test_hive.py (or pytest).

Plain words: automatic self-checks. Each function named test_... tries
one behaviour and uses `assert` ("this must be true") - if any assert is
false the check fails. They need no AI and no internet: the AI is replaced
by canned answers where a whole run is tested.
"""

import os
import tempfile
from pathlib import Path

# Point memory, run files and the knowledge base at throw-away temporary
# folders so tests never touch real data. (This must happen BEFORE the
# imports below.) The knowledge-bases root starts EMPTY on purpose: it
# lets us test running with no knowledge base.
os.environ["CLAUDE_SDLC_ARCHITECT_MEMORY_DIR"] = tempfile.mkdtemp()
os.environ["CLAUDE_SDLC_ARCHITECT_RUNS_ROOT"] = tempfile.mkdtemp()
os.environ["CLAUDE_SDLC_ARCHITECT_KB_ROOT"] = tempfile.mkdtemp()
# Never let a developer's real Jira credentials (in .env) leak into tests:
# variables set here (even empty) take precedence over the .env file.
for _name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"):
    os.environ[_name] = ""

import config
import llm
import knowledge_base.loader as kb
import orchestrator.graph as graph
import tools.issue_tracker as issue_tracker
import tools.jira_client as jira_client
from contextlib import contextmanager
from agents import design as design_agent
from agents import release as release_agent
from agents import requirement as requirement_agent
from agents._common import MAX_OPTION_CHARS, MAX_QUESTION_CHARS, validate_clarifying_questions
from hive import blackboard, events, memory


# ------------------------------------------------------------
# Memory and question shaping
# ------------------------------------------------------------

def test_condense_is_bounded_lossless_in_count_and_keeps_pinned():
    # Store 45 runs with a small limit: the file must shrink, the total
    # run count must stay 45, the "important" run r2 (one a human steered)
    # must survive, a roll-up summary must exist, and a backup file must
    # be created.
    memory.MAX_RECORDS, memory.KEEP_RECENT = 30, 10
    blackboard.post("r2", "human_decisions", "design", "use a managed queue", 1)
    for i in range(45):
        memory.remember({
            "run_id": f"r{i}", "requirement_text": f"notify users episode {i}",
            "requirement": {"title": f"t{i}"}, "run_outcome": "APPROVED_RELEASED",
            "history": [],
        })
    records = memory._load()
    assert len(records) < 30
    assert sum(r.get("runs_summarized", 1) if r.get("kind") == "summary" else 1 for r in records) == 45
    assert any(r["run_id"] == "r2" for r in records)
    assert any(r.get("kind") == "summary" for r in records)
    assert list(config.MEMORY_DIR.glob("index.jsonl.bak-*"))


def test_questions_shortened():
    # Over-long questions/options get trimmed to the allowed length, and
    # the recommended answer is still one of the options.
    r = {"clarifying_questions": [{"question": "Ship now? " + "Detail. " * 80,
                                   "options": ["A " * 100, "B"], "recommended_option": "A " * 100}]}
    validate_clarifying_questions(r, "t")
    q = r["clarifying_questions"][0]
    assert len(q["question"]) <= MAX_QUESTION_CHARS and q["question"].startswith("Ship now?")
    assert all(len(o) <= MAX_OPTION_CHARS for o in q["options"])
    assert q["recommended_option"] in q["options"]


# ------------------------------------------------------------
# Knowledge base: optional, creatable, augmentable
# ------------------------------------------------------------

def test_empty_knowledge_base_is_fine():
    # No documents -> an empty list, and the prompt text tells the agents
    # to design from scratch rather than saying "nothing found".
    empty = Path(tempfile.mkdtemp())
    assert kb.load_all(empty) == []
    assert kb.load_all(empty / "does-not-exist") == []
    assert "greenfield" in kb.format_full_kb_for_prompt([]).lower()
    assert "greenfield" in kb.format_overview_for_prompt([]).lower()


def test_kb_create_then_augment_never_overwrites():
    d = Path(tempfile.mkdtemp()) / "new-folder"  # folder does not exist yet
    kb.create_doc("Todo API", "# todo-api\n\n- **Purpose:** keeps todos", d)
    assert (d / "todo-api.md").is_file()
    # Creating the same one again must refuse, not overwrite.
    try:
        kb.create_doc("todo-api", "# something else", d)
        raise AssertionError("expected FileExistsError")
    except FileExistsError:
        pass
    kb.augment_doc("todo-api", "- exposes `GET /todos`", run_id="r9", kb_dir=d)
    text = (d / "todo-api.md").read_text()
    assert text.startswith("# todo-api")                 # original text untouched
    assert "## Update" in text and "run r9" in text and "GET /todos" in text
    # And the loader can read what the agent wrote.
    docs = kb.load_all(d)
    assert [x.name for x in docs] == ["todo-api"] and docs[0].purpose == "keeps todos"


def test_kb_names_cannot_escape_the_folder():
    d = Path(tempfile.mkdtemp())
    path = kb.create_doc("../../evil", "# x", d)
    assert path.parent == d                               # stayed inside the folder
    try:
        kb.create_doc("///", "# x", d)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_apply_updates_is_forgiving_and_isolates_failures():
    d = Path(tempfile.mkdtemp())
    results = kb.apply_updates(
        [
            {"application": "a", "action": "create", "content": "# a"},
            {"application": "a", "action": "create", "content": "- more"},   # exists -> appended
            {"application": "b", "action": "augment", "content": "# b"},     # missing -> created
            {"application": "c", "action": "delete", "content": "x"},       # unknown action
            {"application": "!!!", "action": "create", "content": "# bad"},  # invalid name
        ],
        run_id="r1", kb_dir=d,
    )
    assert [r["status"] for r in results] == ["applied", "applied", "applied", "skipped", "failed"]
    assert [r["action"] for r in results[:3]] == ["create", "augment", "create"]
    assert "more" in (d / "a.md").read_text()


# ------------------------------------------------------------
# Design agent
# ------------------------------------------------------------

def _design(**over):
    base = {
        "change_required": True, "affected_applications": [], "unaffected_applications_considered": [],
        "new_applications_needed": [{"name": "todo-api", "purpose": "p", "justification": "j"}],
        "cross_service_contracts": [], "dependency_order": ["todo-api"], "test_strategy": "t",
        "architecture_risks": ["r"], "knowledge_base_updates": [], "clarifying_questions": [],
    }
    base.update(over)
    return base


def test_design_from_scratch_validates_with_no_knowledge_base():
    # With nothing known, a new application is fine...
    design_agent._validate(_design(), [])
    # ...but claiming to "affect" an application that is neither known nor
    # proposed as new is rejected.
    try:
        design_agent._validate(_design(affected_applications=[{"name": "ghost"}]), [])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_design_kb_updates_are_cleaned():
    good = {"application": "todo-api", "action": "create", "content": "# todo-api", "reason": "r"}
    r = _design(knowledge_base_updates=[
        good,
        {"application": "x", "action": "explode", "content": "c"},   # bad action
        {"application": "", "action": "create", "content": "c"},      # no name
        {"application": "y", "action": "augment", "content": "  "},   # no content
        "garbage",
    ])
    design_agent._clean_kb_updates(r)
    assert r["knowledge_base_updates"] == [good]
    # No change needed -> nothing is recorded in the knowledge base.
    r2 = _design(change_required=False, knowledge_base_updates=[good])
    design_agent._clean_kb_updates(r2)
    assert r2["knowledge_base_updates"] == []


# ------------------------------------------------------------
# Release agent + Jira hierarchy
# ------------------------------------------------------------

def _story(sid="S1", component="todo-api", **over):
    s = {
        "id": sid, "component": component, "title": f"Story {sid}", "description": "d",
        "acceptance_criteria": ["works"], "blocked_by": [], "team": "", "priority": "High",
        "story_points": 3, "labels": ["new-service"],
        "tasks": [{"title": "Build it", "description": "", "subtasks": [{"title": "write code"}]}],
    }
    s.update(over)
    return s


def _release(stories):
    return {"rollout_stages": [{"stage": 1}], "stories": stories}


def _expect_release_error(stories, expected=("todo-api",)):
    try:
        release_agent._validate(_release(stories), set(expected))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_release_validation_enforces_the_hierarchy():
    release_agent._validate(_release([_story()]), {"todo-api"})        # valid
    _expect_release_error([_story(tasks=[])])                           # story needs tasks
    _expect_release_error([_story(tasks=[{"title": "t", "subtasks": []}])])  # task needs subtasks
    _expect_release_error([_story(component="other")])                  # component out of scope
    _expect_release_error([_story()], expected=("todo-api", "web-ui"))  # web-ui has no story
    _expect_release_error([_story(blocked_by=["S9"])])                  # unknown blocker
    _expect_release_error([_story(blocked_by=["S1"])])                  # blocks itself
    _expect_release_error([_story(), _story()])                         # duplicate id
    _expect_release_error([_story(priority="Urgent")])                  # bad priority


def test_issue_tracker_writes_epic_story_task_subtask():
    out = Path(tempfile.mkdtemp())
    stories = [_story("S1"), _story("S2", blocked_by=["S1"])]
    result = issue_tracker.write_release(out, {"title": "E", "description": "d"}, stories, "20260101-120000-abc")
    assert result["counts"] == {"stories": 2, "tasks": 2, "subtasks": 2}
    s2 = result["stories"][1]
    assert s2["blocked_by"] == [result["stories"][0]["ticket_id"]]       # S1 became a real issue id
    assert s2["tasks"][0]["subtasks"][0]["ticket_id"].endswith("-T1-ST1")
    # One .md and one .json for each of: epic, 2 stories, 2 tasks, 2 subtasks.
    assert len(list(out.glob("*.md"))) == 7 and len(list(out.glob("*.json"))) == 7


# ------------------------------------------------------------
# A whole pipeline run, from scratch, with the AI replaced by canned answers
# ------------------------------------------------------------

def _fake_llm(responses, log=None):
    # Replaces the real AI with canned answers. If `log` is given, every
    # prompt that WOULD have been sent to the AI is recorded as (agent, prompt).
    def fake(prompt, agent_name, required_keys=None, system=None, max_repair_attempts=2, model=None, validate=None):
        if log is not None:
            log.append((agent_name, prompt))
        out = responses[agent_name]
        missing = set(required_keys or []) - set(out)
        assert not missing, f"{agent_name} canned answer lacks {missing}"
        if validate is not None:
            validate(dict(out))          # a canned answer that fails the checks fails the agent, as in real use
        return dict(out)
    return fake


@contextmanager
def _kb_root():
    # Point the knowledge-bases root at a fresh empty folder for one test.
    old = config.KNOWLEDGE_BASES_ROOT
    config.KNOWLEDGE_BASES_ROOT = Path(tempfile.mkdtemp())
    try:
        yield config.KNOWLEDGE_BASES_ROOT
    finally:
        config.KNOWLEDGE_BASES_ROOT = old


def _new_kb_canned(updates=None):
    # A from-scratch run whose design proposes one knowledge-base document.
    update = {"application": "todo-api", "action": "create", "reason": "new service",
              "content": "# todo-api\n\n- **Purpose:** keeps todos\n\n## Depends on (calls)\n\n- none"}
    canned = _scratch_canned("from_scratch")
    canned["Requirement Agent"]["title"] = "Todo app"
    canned["Design Agent"] = _design(knowledge_base_updates=[update] if updates is None else updates)
    return canned


def _run_new_kb(**state):
    return _run_pipeline([], "Build a todo app from scratch", _new_kb_canned(), force_from_scratch=True, **state)[0]


def test_knowledge_bases_can_be_listed_created_and_looked_up():
    with _kb_root() as root:
        assert kb.list_knowledge_bases() == []
        (root / "__pycache__").mkdir(); (root / ".hidden").mkdir(); (root / "loose.md").write_text("x")
        path = kb.create_knowledge_base("My New KB!")                    # name is made folder-safe
        assert path == root / "my-new-kb" and path.is_dir()
        kb.create_doc("a", "# a", path)
        assert [(k.name, k.application_count) for k in kb.list_knowledge_bases()] == [("my-new-kb", 1)]
        assert kb.kb_path("my-new-kb") == path
        for bad in ("nope", "../x", ""):
            try:
                kb.kb_path(bad); raise AssertionError("expected FileNotFoundError")
            except FileNotFoundError:
                pass
        try:
            kb.create_knowledge_base("My New KB"); raise AssertionError("expected FileExistsError")
        except FileExistsError:
            pass                                                         # never reuse a name
        assert kb.suggest_kb_name("My New KB") == "my-new-kb-2" and kb.suggest_kb_name("Fresh Idea") == "fresh-idea"
        assert kb.normalise_kb_name("  Hello World! ") == "hello-world" and kb.normalise_kb_name("!!!") == ""


def test_new_knowledge_base_is_only_created_if_the_user_says_yes():
    with _kb_root() as root:
        # No answer at all, an up-front "yes" (discarded: it predates the design), and an
        # explicit "no" all leave the knowledge-bases folder empty.
        for state in ({}, {"kb_approved": True}, {"kb_confirm": lambda preview: None},
                      {"kb_confirm": lambda preview: False}):
            final = _run_new_kb(**state)
            assert final["kb_result"]["status"] == "declined", state
            assert kb.list_knowledge_bases() == []

        asked = []
        final = _run_new_kb(kb_confirm=lambda p: asked.append(p) or "todo-kb")
        assert final["kb_result"]["status"] == "saved" and final["kb_result"]["created_new_kb"]
        assert [k.name for k in kb.list_knowledge_bases()] == ["todo-kb"]
        assert (root / "todo-kb" / "todo-api.md").is_file()
        assert [d.name for d in kb.load_all(root / "todo-kb")] == ["todo-api"]       # loadable next time
        # What the user was shown: a NEW knowledge base, a suggested name, the documents.
        p = asked[0]
        assert p["mode"] == "new" and p["suggested_name"] == "todo-app" and p["updates"][0]["application"] == "todo-api"


def test_new_knowledge_base_name_cannot_collide_with_an_existing_one():
    with _kb_root() as root:
        existing = kb.create_knowledge_base("todo-kb")
        kb.create_doc("keep-me", "# keep-me", existing)
        final = _run_new_kb(kb_confirm=lambda p: "todo-kb")
        assert final["kb_result"]["status"] == "failed" and "already exists" in final["kb_result"]["error"]
        assert [p.name for p in existing.iterdir()] == ["keep-me.md"]               # untouched
        assert final["run_outcome"] == "APPROVED_RELEASED"                          # the run itself survives


def test_nothing_to_ask_when_the_design_proposes_no_knowledge_base_changes():
    with _kb_root():
        asked = []
        final = _run_pipeline([], "Build a todo app", _new_kb_canned(updates=[]), force_from_scratch=True,
                              kb_confirm=lambda p: asked.append(p) or "x")[0]
        assert final["kb_result"]["status"] == "none" and asked == []
        assert kb.list_knowledge_bases() == []


def test_selected_knowledge_base_is_only_changed_after_approval():
    with _kb_root() as root:
        folder = kb.create_knowledge_base("platform")
        kb.create_doc("user-service", "# user-service\n\n- **Purpose:** keeps users", folder)
        before = (folder / "user-service.md").read_text()
        docs = kb.load_all(folder)

        canned = _scratch_canned("extend_existing")
        canned["Design Agent"] = _design(
            affected_applications=[{"name": "user-service", "role_in_change": "r", "reason": "r"}],
            new_applications_needed=[{"name": "reminder-service", "purpose": "p", "justification": "j"}],
            dependency_order=["user-service", "reminder-service"],
            knowledge_base_updates=[
                {"application": "user-service", "action": "augment", "content": "- stores reminder prefs", "reason": "r"},
                {"application": "reminder-service", "action": "create", "content": "# reminder-service", "reason": "r"},
            ])
        canned["Release Agent"]["stories"] = [_story("S1", component="user-service"),
                                              _story("S2", component="reminder-service", blocked_by=["S1"])]

        def run(**state):
            return _run_pipeline(docs, "Add reminders", canned, kb_name="platform", **state)[0]

        final = run()                                                    # nobody answered
        assert final["kb_result"]["status"] == "declined" and (folder / "user-service.md").read_text() == before
        assert not (folder / "reminder-service.md").exists()

        asked = []
        final = run(kb_confirm=lambda p: asked.append(p) or True)
        assert asked[0]["mode"] == "existing" and asked[0]["kb_name"] == "platform"
        assert final["kb_result"]["status"] == "saved" and not final["kb_result"]["created_new_kb"]
        text = (folder / "user-service.md").read_text()
        assert text.startswith(before.rstrip()) and "stores reminder prefs" in text  # appended, not rewritten
        assert (folder / "reminder-service.md").is_file()
        assert [k.name for k in kb.list_knowledge_bases()] == ["platform"]           # no extra knowledge base


def test_a_from_scratch_request_is_saved_as_a_new_knowledge_base_even_if_one_was_selected():
    # The user picked an existing knowledge base, but the Requirement agent judged the request
    # to be from-scratch: its documents must not be mixed into the selected one.
    state = {"kb_name": "platform", "from_scratch": True,
             "design": {"knowledge_base_updates": [{"application": "a", "action": "create", "content": "# a"}]},
             "requirement": {"title": "Brand new"}}
    with _kb_root():
        kb.create_knowledge_base("platform")
        assert graph.kb_save_preview(state)["mode"] == "new"
        assert graph.kb_save_preview({**state, "from_scratch": False})["mode"] == "existing"


def test_pipeline_skips_release_when_no_change_is_needed():
    requirement = {
        "title": "t", "business_goal": "g", "scope_summary": "s",
        "acceptance_criteria": ["a"], "out_of_scope": [], "build_mode": "from_scratch",
        "clarifying_questions": [],
    }
    no_change = _design(change_required=False, new_applications_needed=[], dependency_order=[])
    original = llm.call_llm_json
    llm.call_llm_json = _fake_llm({"Requirement Agent": requirement, "Design Agent": no_change})
    try:
        final = graph.build_graph().invoke({
            "run_id": graph.new_run_id(), "requirement_text": "Already supported",
            "applications": [], "iteration": 0, "history": [], "agent_models": {},
        })
    finally:
        llm.call_llm_json = original
    assert final["run_outcome"] == "NO_CHANGE_REQUIRED"
    assert not final.get("release_result")


# ------------------------------------------------------------
# From-scratch builds must not touch the existing knowledge base
# ------------------------------------------------------------

SECRET = "ZZ-EXISTING-SYSTEM-MARKER"


def _existing_kb():
    # An "old" knowledge base with one distinctive document.
    d = Path(tempfile.mkdtemp())
    kb.create_doc("user-service", f"# user-service\n\n- **Purpose:** {SECRET} keeps user profiles", d)
    return d, kb.load_all(d)


def _scratch_canned(build_mode):
    requirement = {
        "title": "Todo", "business_goal": "g", "scope_summary": "s", "acceptance_criteria": ["a"],
        "out_of_scope": [], "build_mode": build_mode, "clarifying_questions": [],
    }
    release = {
        "epic": {"title": "E", "description": "d"}, "stories": [_story()],
        "rollout_stages": [{"stage": 1}], "rollout_narrative": "n", "monitoring_plan": [],
        "clarifying_questions": [],
    }
    return {"Requirement Agent": requirement, "Design Agent": _design(), "Release Agent": release}


def _run_pipeline(docs, text, canned, **state):
    log = []
    original = llm.call_llm_json
    llm.call_llm_json = _fake_llm(canned, log)
    try:
        final = graph.build_graph().invoke({
            "run_id": graph.new_run_id(), "requirement_text": text, "applications": docs,
            "iteration": 0, "history": [], "agent_models": {}, **state,
        })
    finally:
        llm.call_llm_json = original
    return final, log


def _remember_old_project():
    # Put a past run about the SAME topic in cross-run memory, so we can see
    # whether it is (wrongly) recalled into a prompt.
    memory.remember({
        "run_id": "old-run", "requirement_text": "Build a todo app with notify users reminders",
        "requirement": {"title": "Old todo"}, "run_outcome": "APPROVED_RELEASED", "history": [],
        "design": {"affected_applications": [{"name": "user-service"}], "dependency_order": ["user-service"],
                   "architecture_risks": [f"{SECRET} risk"]},
    })


def test_control_extending_an_existing_system_does_use_the_knowledge_base():
    # Control: without from-scratch, the old knowledge base and memory DO
    # reach the Design prompt. This proves the isolation tests below could
    # actually fail.
    _, docs = _existing_kb()
    _remember_old_project()
    _, log = _run_pipeline(docs, "Build a todo app with notify users reminders",
                           _scratch_canned("extend_existing"))
    design_prompt = next(p for a, p in log if a == "Design Agent")
    assert SECRET in design_prompt and "user-service" in design_prompt


def test_user_flag_withholds_knowledge_base_and_memory_from_every_agent():
    _, docs = _existing_kb()
    _remember_old_project()
    final, log = _run_pipeline(docs, "Build a todo app with notify users reminders",
                               _scratch_canned("extend_existing"), force_from_scratch=True)
    assert {a for a, _ in log} == {"Requirement Agent", "Design Agent", "Release Agent"}
    for agent, prompt in log:
        assert SECRET not in prompt and "user-service" not in prompt, f"{agent} saw the old system"
    assert final["from_scratch"] is True
    assert final["requirement"]["build_mode"] == "from_scratch"     # the user's choice wins


def test_requirement_agent_can_detect_a_from_scratch_request():
    # No flag: the Requirement agent classifies the request, and from then
    # on the Design and Release stages get nothing from the old system.
    _, docs = _existing_kb()
    _remember_old_project()
    final, log = _run_pipeline(docs, "Build a todo app with notify users reminders",
                               _scratch_canned("from_scratch"))
    assert final["from_scratch"] is True
    for agent, prompt in log:
        if agent != "Requirement Agent":   # it only sees the request when deciding
            assert SECRET not in prompt and "user-service" not in prompt, f"{agent} saw the old system"


def test_design_in_scratch_mode_rejects_existing_applications():
    # Even if the AI names an existing application anyway, validation fails.
    _, docs = _existing_kb()
    bad = _design(affected_applications=[{"name": "user-service", "role_in_change": "r", "reason": "r"}])
    original = llm.call_llm_json
    llm.call_llm_json = _fake_llm({"Design Agent": bad})
    try:
        design_agent.run({"title": "t"}, docs, from_scratch=True)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    finally:
        llm.call_llm_json = original


def test_scratch_knowledge_base_writes_never_touch_existing_documents():
    d, _ = _existing_kb()
    before = (d / "user-service.md").read_text()
    results = kb.apply_updates(
        [
            {"application": "user-service", "action": "create", "content": "- scratch design"},
            {"application": "user-service", "action": "augment", "content": "- scratch design"},
            {"application": "todo-api", "action": "create", "content": "# todo-api"},
        ],
        run_id="r1", kb_dir=d, create_only=True,
    )
    assert [r["status"] for r in results] == ["skipped", "skipped", "applied"]
    assert (d / "user-service.md").read_text() == before          # untouched
    assert (d / "todo-api.md").is_file()                          # new one created


def test_requirement_build_mode_is_validated_and_user_choice_wins():
    base = {"title": "t", "business_goal": "g", "scope_summary": "s", "acceptance_criteria": ["a"],
            "out_of_scope": [], "clarifying_questions": []}
    original = llm.call_llm_json
    try:
        llm.call_llm_json = _fake_llm({"Requirement Agent": {**base, "build_mode": "maybe"}})
        try:
            requirement_agent.run("x", [])
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        llm.call_llm_json = _fake_llm({"Requirement Agent": {**base, "build_mode": "extend_existing"}})
        assert requirement_agent.run("x", [], from_scratch=True)["build_mode"] == "from_scratch"
    finally:
        llm.call_llm_json = original


# ------------------------------------------------------------
# Real Jira: tested against a FAKE Jira (no network)
# ------------------------------------------------------------

TOKEN = "SECRET-TOKEN-123"


class _Resp:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body or {}, headers or {}, text

    def json(self):
        return self._body


class FakeJira:
    """Just enough of Jira's REST API to check our client's behaviour."""

    def __init__(self, reject=None, fail_after=None, rate_limit_first=False, bad_auth=False):
        self.issues, self.links, self.calls = {}, [], []
        self.reject = reject or {}           # field id -> message Jira answers with
        self.fail_after = fail_after         # refuse (403) once this many issues exist
        self.rate_limit_first = rate_limit_first
        self.bad_auth = bad_auth

    def request(self, method, url, json=None, headers=None, auth=None, timeout=None):
        path = url.split("/rest/api/2")[1]
        self.calls.append((method, path, json, auth, dict(headers or {})))
        if self.bad_auth:
            return _Resp(401, {}, text=f"bad credentials for {TOKEN}")
        if self.rate_limit_first:
            self.rate_limit_first = False
            return _Resp(429, headers={"Retry-After": "7"})
        if method == "GET" and path.startswith("/project/"):
            return _Resp(200, {"issueTypes": [
                {"id": "1", "name": "Epic", "subtask": False}, {"id": "2", "name": "Story", "subtask": False},
                {"id": "3", "name": "Task", "subtask": False}, {"id": "4", "name": "Sub-task", "subtask": True}]})
        if method == "GET" and path == "/field":
            return _Resp(200, [{"id": "customfield_10016", "name": "Story point estimate"},
                               {"id": "customfield_10011", "name": "Epic Name"}])
        if method == "POST" and path == "/issue":
            fields = json["fields"]
            if self.fail_after is not None and len(self.issues) >= self.fail_after:
                return _Resp(403, {"errorMessages": ["no permission to create here"]})
            errors = {k: m for k, m in self.reject.items() if k in fields}
            if errors:
                return _Resp(400, {"errors": errors})
            key = f"ARCH-{len(self.issues) + 1}"
            self.issues[key] = fields
            return _Resp(201, {"key": key})
        if method == "POST" and path == "/issueLink":
            self.links.append(json)
            return _Resp(201)
        return _Resp(404)


def _client(fake, email="me@co.com"):
    waits = []
    c = jira_client.JiraClient("https://co.atlassian.net/", "ARCH", TOKEN, email, session=fake, sleep=waits.append)
    c.waits = waits
    return c


def _plan_stories():
    return [_story("S1"), _story("S2", blocked_by=["S1"], title="Second")]


def _publish(client, stories=None, state_path=None, selection=None):
    return client.publish({"title": "Epic", "description": "why"}, stories or _plan_stories(), "run1",
                          state_path, selection=selection)


def test_jira_preview_is_offline_and_never_contains_the_token():
    with _jira_env():
        p = jira_client.preview({"stories": _plan_stories()})
    assert p["configured"] and p["project_key"] == "ARCH" and p["base_url"] == "https://co.atlassian.net"
    assert (p["epics"], p["stories"], p["sub_tasks"], p["steps"]) == (1, 2, 2, 2)
    assert TOKEN not in repr(p)
    with _jira_env(on=False):
        assert jira_client.preview({"stories": []})["configured"] is False


def test_jira_publish_builds_epic_story_subtask_and_blocks_link():
    fake = FakeJira()
    result = _publish(_client(fake))
    keys = list(fake.issues)                                   # creation order
    assert keys[0] == "ARCH-1" and fake.issues["ARCH-1"]["issuetype"] == {"id": "1"}      # Epic first
    story1, sub1 = fake.issues["ARCH-2"], fake.issues["ARCH-3"]
    assert story1["issuetype"] == {"id": "2"} and story1["parent"] == {"key": "ARCH-1"}   # under the epic
    assert sub1["issuetype"] == {"id": "4"} and sub1["parent"] == {"key": "ARCH-2"}       # task -> sub-task
    assert "write code" in sub1["description"]                                           # our subtask -> a step
    assert story1["priority"] == {"name": "High"} and story1["customfield_10016"] == 3.0
    assert "component-todo-api" in story1["labels"] and "arch-run-run1" in story1["labels"]
    assert "Acceptance criteria" in story1["description"] and "works" in story1["description"]
    # S2 is blocked by S1: S1 (outward) "blocks" S2 (inward).
    assert fake.links == [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "ARCH-4"}, "outwardIssue": {"key": "ARCH-2"}}]
    assert result["status"] == "created" and result["counts"] == {"epics": 1, "stories": 2, "sub_tasks": 2}
    assert result["epic"]["url"] == "https://co.atlassian.net/browse/ARCH-1"


def test_jira_cloud_uses_email_and_data_center_uses_bearer_token():
    cloud = FakeJira(); _publish(_client(cloud))
    assert cloud.calls[0][3] == ("me@co.com", TOKEN) and "Authorization" not in cloud.calls[0][4]
    dc = FakeJira(); _publish(_client(dc, email=""))
    assert dc.calls[0][3] is None and dc.calls[0][4]["Authorization"] == f"Bearer {TOKEN}"


def test_jira_drops_optional_fields_a_project_rejects_and_says_so():
    fake = FakeJira(reject={"customfield_10016": "cannot be set", "priority": "invalid priority"})
    result = _publish(_client(fake))
    assert result["status"] == "created"
    assert {d["field"] for d in result["dropped_fields"]} >= {"customfield_10016", "priority"}
    assert "priority" not in fake.issues["ARCH-2"] and "customfield_10016" not in fake.issues["ARCH-2"]


def test_jira_required_field_we_cannot_fill_gives_a_clear_error():
    fake = FakeJira(reject={"summary": "Summary is required"})       # a field we cannot drop
    try:
        _publish(_client(fake))
        raise AssertionError("expected JiraError")
    except jira_client.JiraError as e:
        assert "summary" in str(e).lower()


def test_jira_publish_is_resumable_and_never_duplicates():
    state = Path(tempfile.mkdtemp()) / "jira_created.json"
    fake = FakeJira(fail_after=2)                                      # dies after epic + story 1
    try:
        _publish(_client(fake), state_path=state)
        raise AssertionError("expected JiraError")
    except jira_client.JiraError as e:
        assert e.status == 403 and e.created_count == 2
    assert len(fake.issues) == 2 and state.is_file()
    fake.fail_after = None                                             # Jira recovers; try again
    result = _publish(_client(fake), state_path=state)
    assert len(fake.issues) == 5 and result["resumed"]                 # 1 epic + 2 stories + 2 sub-tasks, no repeats
    assert len({f["summary"] for f in fake.issues.values() if f["issuetype"] == {"id": "2"}}) == 2
    calls_before = len(fake.calls)
    again = _publish(_client(fake), state_path=state)                  # same plan, already done
    assert len(fake.calls) == calls_before and again["status"] == "created"   # no network at all


def test_jira_changed_plan_starts_fresh_and_warns_about_old_issues():
    state = Path(tempfile.mkdtemp()) / "jira_created.json"
    fake = FakeJira()
    _publish(_client(fake), state_path=state)
    n = len(fake.issues)
    result = _publish(_client(fake), stories=[_story("S1", title="Different")], state_path=state)
    assert len(fake.issues) > n and any("still exist in Jira" in w for w in result["warnings"])


def test_jira_rate_limit_is_waited_out_and_retried():
    fake = FakeJira(rate_limit_first=True)
    client = _client(fake)
    _publish(client)
    assert client.waits and client.waits[0] == 7.0 and len(fake.issues) == 5


def test_jira_errors_never_contain_the_token():
    try:
        _publish(_client(FakeJira(bad_auth=True)))
        raise AssertionError("expected JiraError")
    except jira_client.JiraError as e:
        assert TOKEN not in str(e) and "credentials were rejected" in str(e)


def test_jira_project_key_mistakes_get_plain_explanations():
    # An issue key pasted instead of a project key is caught before any call...
    fake = FakeJira()
    c = jira_client.JiraClient("https://co.atlassian.net", "ABC-12", TOKEN, "me@co.com", session=fake)
    try:
        _publish(c)
        raise AssertionError("expected JiraError")
    except jira_client.JiraError as e:
        assert "looks like an issue key" in str(e) and "'ABC'" in str(e) and fake.calls == []

    # ...and an account that can see no projects at all is called out as such.
    class Blind(FakeJira):
        def request(self, method, url, **kw):
            path = url.split("/rest/api/2")[1]
            if path.startswith("/project/search"):
                return _Resp(200, {"values": []})
            if path.startswith("/project/"):
                return _Resp(404, {"errorMessages": ["No project could be found"]})
            return super().request(method, url, **kw)

    try:
        _publish(_client(Blind()))
        raise AssertionError("expected JiraError")
    except jira_client.JiraError as e:
        assert "cannot see ANY project" in str(e) and TOKEN not in str(e)


def _two_task_story(sid="S1"):
    story = _story(sid)
    story["tasks"] = [
        {"title": "First task", "description": "", "subtasks": [{"title": "a"}]},
        {"title": "Second task", "description": "", "subtasks": [{"title": "b"}]},
    ]
    return story


def test_jira_publishes_only_the_selected_tickets_and_the_rest_can_follow_later():
    state = Path(tempfile.mkdtemp()) / "jira_created.json"
    fake = FakeJira()
    first = _publish(_client(fake), state_path=state, selection={"epic", "S1", "S1/T1"})
    assert len(fake.issues) == 3 and fake.links == []                          # S2 not sent, so no link
    assert [s["id"] for s in first["stories"]] == ["S1"]
    assert first["not_sent"]["stories"] == [{"id": "S2", "title": "Second"}]
    assert first["counts"] == {"epics": 1, "stories": 1, "sub_tasks": 1}

    # Later: send the rest. Epic and S1 are NOT created again, and the link now appears.
    second = _publish(_client(fake), state_path=state, selection={"S2", "S2/T1"})
    assert len(fake.issues) == 5 and second["counts"] == {"epics": 1, "stories": 2, "sub_tasks": 2}
    assert len(fake.links) == 1 and second["not_sent"] == {"epic": False, "stories": [], "sub_tasks": []}
    calls = len(fake.calls)
    _publish(_client(fake), state_path=state)                                  # "everything": already there
    assert len(fake.calls) == calls


def test_jira_can_create_just_some_of_a_storys_sub_tasks():
    fake = FakeJira()
    result = _publish(_client(fake), stories=[_two_task_story()], selection={"epic", "S1", "S1/T2"})
    subs = [f for f in fake.issues.values() if f["issuetype"] == {"id": "4"}]
    assert len(fake.issues) == 3 and [f["summary"] for f in subs] == ["Second task"]
    assert result["not_sent"]["sub_tasks"] == [{"story": "Story S1", "title": "First task"}]


def test_jira_stories_can_be_created_without_an_epic():
    fake = FakeJira()
    result = _publish(_client(fake), selection={"S1", "S1/T1"})
    story = next(f for f in fake.issues.values() if f["issuetype"] == {"id": "2"})
    assert "parent" not in story and result["epic"] is None and result["not_sent"]["epic"] is True
    assert any("No epic was created" in w for w in result["warnings"])


def test_jira_selection_mistakes_send_nothing():
    for selection, text in ((set(), "No tickets were selected"), ({"nope"}, "No tickets were selected"),
                            ({"S1/T1"}, "is not in Jira")):                 # sub-task without its story
        fake = FakeJira()
        try:
            _publish(_client(fake), selection=selection)
            raise AssertionError("expected JiraError")
        except jira_client.JiraError as e:
            assert text in str(e) and fake.issues == {} and fake.calls == [], (selection, str(e))


def test_jira_preview_lists_the_tickets_the_user_can_pick():
    with _jira_env():
        p = jira_client.preview({"epic": {"title": "E"}, "stories": [_two_task_story("S1"), _story("S2")]})
    assert p["epic_title"] == "E"
    assert [(i["key"], [t["key"] for t in i["tasks"]]) for i in p["items"]] == [
        ("S1", ["S1/T1", "S1/T2"]), ("S2", ["S2/T1"])]
    assert jira_client.plan_keys({"stories": [_two_task_story("S1"), _story("S2")]}) == [
        "epic", "S1", "S1/T1", "S1/T2", "S2", "S2/T1"]
    assert TOKEN not in repr(p)


def test_pipeline_sends_only_the_tickets_the_user_chose():
    chosen = {"epic", "S1", "S1/T1"}
    fake = FakeJira()
    final = _pipeline_with_jira(fake, jira_confirm=lambda p: chosen)
    assert len(fake.issues) == 3 and final["jira_result"]["counts"] == {"epics": 1, "stories": 1, "sub_tasks": 1}
    assert final["run_outcome"] == "RELEASED_TO_JIRA"

    fake = FakeJira()                                                           # True = everything
    assert len(_pipeline_with_jira(fake, jira_confirm=lambda p: True)["jira_result"]["stories"]) == 2

    fake = FakeJira()                                                           # nothing ticked
    final = _pipeline_with_jira(fake, jira_confirm=lambda p: set())
    assert fake.calls == [] and final["jira_result"]["status"] == "declined"
    assert "No tickets were selected" in final["jira_result"]["reason"]

    fake = FakeJira()                                                           # a choice made before the plan existed
    final = _pipeline_with_jira(fake, jira_approved=True, jira_selection=["S1"])
    assert fake.calls == [] and final["jira_result"]["status"] == "declined"


def test_cli_choice_parsing_never_guesses():
    import cli
    p = {"items": [{"key": "S1", "tasks": [{"key": "S1/T1"}, {"key": "S1/T2"}]},
                   {"key": "S2", "tasks": [{"key": "S2/T1"}]}]}
    assert cli.parse_jira_choice("a", p) is True and cli.parse_jira_choice("YES", p) is True
    assert cli.parse_jira_choice("2", p) == {"epic", "S2", "S2/T1"}
    assert cli.parse_jira_choice("1, 2", p) == {"epic", "S1", "S1/T1", "S1/T2", "S2", "S2/T1"}
    assert cli.parse_jira_choice("1,x", p) == {"S1", "S1/T1", "S1/T2"}          # x = no epic
    for nothing in ("", "n", "no", "x", "3", "0", "1,9", "1;2", "banana"):      # typos create nothing
        assert cli.parse_jira_choice(nothing, p) is None, nothing


def _editable_state():
    # A pipeline state that has finished release planning and written its local files.
    run_id = graph.new_run_id()
    plan = {"epic": {"title": "Todo epic", "description": "why"},
            "stories": [_two_task_story("S1"), _story("S2", blocked_by=["S1"], title="Second")],
            "rollout_stages": [{"stage": 1}], "rollout_narrative": "n"}
    result = issue_tracker.write_release(config.RUNS_ROOT / run_id / "jira_mock", plan["epic"], plan["stories"], run_id)
    return {"run_id": run_id, "release_plan": plan, "release_result": result, "history": []}


def _local_text(state):
    return "\n".join(p.read_text() for p in sorted((config.RUNS_ROOT / state["run_id"] / "jira_mock").glob("*.md")))


def test_ticket_wording_can_be_edited_and_the_local_files_follow():
    import copy
    state = _editable_state()
    edited = copy.deepcopy(state["release_plan"])
    edited["epic"]["title"] = "Reworded epic"
    edited["stories"][0]["title"] = "Reworded story"
    edited["stories"][0]["description"] = "A clearer description"
    edited["stories"][0]["acceptance_criteria"] = ["It works, said plainly"]
    edited["stories"][0]["tasks"][1]["title"] = "Reworded task"
    edited["stories"][0]["tasks"][0]["subtasks"] = [{"title": "a"}, {"title": "a brand new step"}]
    assert "Reworded" not in _local_text(state)

    update = graph.apply_ticket_edits(state, edited)
    assert update["release_plan"] == edited
    changes = update["history"][-1]["result"]["changes"]
    assert sorted(changes) == sorted(["epic.title", "S1.title", "S1.description", "S1.acceptance_criteria",
                                      "S1/T2.title", "S1/T1.steps"])
    text = _local_text(state)                                  # the local files now say the same thing
    assert all(w in text for w in ("Reworded epic", "Reworded story", "A clearer description",
                                   "It works, said plainly", "Reworded task", "a brand new step"))
    new_counts, old_counts = update["release_result"]["counts"], state["release_result"]["counts"]
    assert (new_counts["stories"], new_counts["tasks"]) == (old_counts["stories"], old_counts["tasks"])  # same shape
    assert new_counts["subtasks"] == old_counts["subtasks"] + 1                                          # one added step
    assert any(e["kind"] == "plan_edited" and e["changes"] == 6 for e in events.read(state["run_id"]))
    assert graph.apply_ticket_edits({**state, **update}, edited) == {}                # nothing new to change


def test_ticket_edits_may_only_change_words():
    import copy
    state = _editable_state()
    before = _local_text(state)

    def reject(mutate, expected):
        edited = copy.deepcopy(state["release_plan"])
        mutate(edited)
        try:
            graph.apply_ticket_edits(state, edited)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert expected in str(e), str(e)
        assert _local_text(state) == before                       # a refused edit changes nothing at all

    reject(lambda p: p["stories"][0].update(priority="Lowest"), "wording")
    reject(lambda p: p["stories"][0].update(story_points=13), "wording")
    reject(lambda p: p["stories"][0].update(labels=["x"]), "wording")
    reject(lambda p: p["stories"][1].update(blocked_by=[]), "wording")
    reject(lambda p: p["stories"][0].update(component="other"), "wording")
    reject(lambda p: p["stories"].pop(), "wording")                              # remove a story
    reject(lambda p: p["stories"][0]["tasks"].pop(), "wording")                  # remove a task
    reject(lambda p: p.update(rollout_narrative="changed"), "wording")
    reject(lambda p: p["epic"].update(title="  "), "Every ticket needs a title")
    reject(lambda p: p["stories"][0].update(title=""), "Every ticket needs a title")
    reject(lambda p: p["stories"][0]["tasks"][0].update(title=""), "Every ticket needs a title")
    reject(lambda p: p["stories"][0]["tasks"][0]["subtasks"].append({"title": " "}), "Every ticket needs a title")


def test_reworded_tickets_are_what_gets_sent_to_jira():
    import copy
    state = _editable_state()
    edited = copy.deepcopy(state["release_plan"])
    edited["stories"][0]["title"] = "Reworded story"
    edited["stories"][0]["description"] = "Reworded description"
    edited["stories"][0]["tasks"][0]["subtasks"] = [{"title": "reworded step"}]
    state.update(graph.apply_ticket_edits(state, edited))
    state.update(jira_approved=True, jira_selection=None)

    fake = FakeJira()
    with _jira_env(), _fake_jira_in_pipeline(fake):
        result = graph.jira_publish_node(state)["jira_result"]
    assert result["status"] == "created"
    story = next(f for f in fake.issues.values() if f["summary"] == "Reworded story")
    assert "Reworded description" in story["description"]
    assert any("reworded step" in f["description"] for f in fake.issues.values())
    assert not any(f["summary"] == "Story S1" for f in fake.issues.values())     # the old wording is gone


# ------------------------------------------------------------
# Choosing which agents run, and architectural direction in between
# ------------------------------------------------------------

DIRECTION = "Use the existing event bus; no new databases."


def _agents(log):
    return {agent for agent, _ in log}


def _prompt(log, agent):
    return next(p for a, p in log if a == agent)


def test_user_text_can_be_used_as_the_requirement_without_the_requirement_agent():
    req = graph.direct_requirement("Add reminders to the todo app\nEmail users a day before.", False)
    assert req["title"] == "Add reminders to the todo app" and req["scope_summary"].endswith("a day before.")
    assert req["build_mode"] == "extend_existing" and req["acceptance_criteria"] == []
    assert graph.direct_requirement("x", True)["build_mode"] == "from_scratch"

    canned = _scratch_canned("extend_existing")
    del canned["Requirement Agent"]                   # would raise KeyError if the agent were called
    final, log = _run_pipeline([], "Build a todo app", canned, start_at="design")
    assert _agents(log) == {"Design Agent", "Release Agent"}
    assert final["requirement_skipped"] and final["requirement"]["scope_summary"] == "Build a todo app"
    assert final["run_outcome"] == "APPROVED_RELEASED"
    assert "Build a todo app" in _prompt(log, "Design Agent")     # the user's own words reach the Design agent

    final, log = _run_pipeline([], "Build a todo app", canned, start_at="design", force_from_scratch=True)
    assert final["from_scratch"] and final["requirement"]["build_mode"] == "from_scratch"


def test_run_can_stop_after_the_requirement_agent():
    final, log = _run_pipeline([], "Build a todo app", _scratch_canned("from_scratch"), stop_after="requirement")
    assert _agents(log) == {"Requirement Agent"}
    assert final["run_outcome"] == "REQUIREMENT_ONLY" and "design" not in final and "release_plan" not in final


def test_run_can_stop_after_the_design_agent_and_still_offers_the_knowledge_base():
    with _kb_root():
        asked = []
        final, log = _run_pipeline([], "Build a todo app", _new_kb_canned(), force_from_scratch=True,
                                   stop_after="design", kb_confirm=lambda p: asked.append(p) or "design-only-kb")
        assert _agents(log) == {"Requirement Agent", "Design Agent"}          # no Release agent, no Jira
        assert final["run_outcome"] == "DESIGN_ONLY"
        assert not final.get("release_plan") and not final.get("release_result") and not final.get("jira_result")
        assert len(asked) == 1 and final["kb_result"]["status"] == "saved"        # the design's documents can still be saved
        assert [k.name for k in kb.list_knowledge_bases()] == ["design-only-kb"]

    # A design that finds nothing to change ends as "no change", whatever was chosen.
    no_change = _scratch_canned("from_scratch")
    no_change["Design Agent"] = _design(change_required=False, new_applications_needed=[], dependency_order=[])
    final, _ = _run_pipeline([], "Already supported", no_change, stop_after="design")
    assert final["run_outcome"] == "NO_CHANGE_REQUIRED"


def test_routers_follow_the_chosen_stopping_point():
    assert graph.requirement_router({}) == "design"
    assert graph.requirement_router({"stop_after": "release"}) == "design"
    assert graph.requirement_router({"stop_after": "requirement"}) == "finalize"
    assert graph.design_router({"change_required": True}) == "release_planning"
    assert graph.design_router({"change_required": True, "stop_after": "design"}) == "kb_save"
    assert graph.design_router({"change_required": False, "stop_after": "design"}) == "finalize"


def test_architectural_direction_reaches_only_the_design_agent():
    final, log = _run_pipeline([], "Build a todo app", _scratch_canned("extend_existing"),
                               architecture_direction=f"  {DIRECTION}  ")
    design_prompt = _prompt(log, "Design Agent")
    assert "ARCHITECTURAL DIRECTION FROM THE USER (binding)" in design_prompt and DIRECTION in design_prompt
    for agent in ("Requirement Agent", "Release Agent"):
        assert DIRECTION not in _prompt(log, agent), agent
    run_id = final["run_id"]
    shown = [e for e in blackboard.read(run_id)["human_decisions"] if e["stage"] == "direction"]
    assert [(e["text"], e["status"]) for e in shown] == [(DIRECTION, "active")]       # visible on the whiteboard
    assert any(e["kind"] == "direction" for e in events.read(run_id))

    _, log = _run_pipeline([], "Build a todo app", _scratch_canned("extend_existing"))     # none given
    assert "ARCHITECTURAL DIRECTION" not in _prompt(log, "Design Agent")
    _, log = _run_pipeline([], "Build a todo app", _scratch_canned("extend_existing"),
                           architecture_direction="   ")                                    # blank = none
    assert "ARCHITECTURAL DIRECTION" not in _prompt(log, "Design Agent")


def test_changing_the_direction_replaces_it_rather_than_stacking():
    original = llm.call_llm_json
    llm.call_llm_json = _fake_llm({"Design Agent": _design()})
    try:
        state = {"run_id": graph.new_run_id(), "requirement_text": "t", "requirement": {"title": "t"}, "applications": [],
                 "iteration": 0, "history": [], "architecture_direction": "First idea"}
        graph.design_node(state)
        state["architecture_direction"] = "Second idea"
        graph.design_node(state)
    finally:
        llm.call_llm_json = original
    active = [e["text"] for e in blackboard.read(state["run_id"])["human_decisions"]
              if e["stage"] == "direction" and e["status"] == "active"]
    assert active == ["Second idea"]


def test_cli_rejects_a_run_that_stops_before_it_starts():
    import cli
    for start, stop in (("requirement", "requirement"), ("requirement", "design"), ("requirement", "release"),
                        ("design", "design"), ("design", "release")):
        cli.check_scope(start, stop)                                  # all fine
    try:
        cli.check_scope("design", "requirement")
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "nothing to run" in str(e)


# ------------------------------------------------------------
# A bad model answer is sent back for correction instead of ending the run
# ------------------------------------------------------------

@contextmanager
def _scripted_model(answers, prompts):
    # Replace the real AI call underneath call_llm_json with a script of answers,
    # recording every prompt it is sent. (The retry logic itself stays real.)
    queue = list(answers)

    async def fake_call_once(prompt, system, schema, model=None):
        prompts.append(prompt)
        return queue.pop(0)

    original = llm._call_once
    llm._call_once = fake_call_once
    try:
        yield
    finally:
        llm._call_once = original


def _requires_items(result):
    if not result.get("items"):
        raise ValueError("items is empty; give at least one")


def test_a_bad_answer_is_retried_with_the_reason_and_the_run_survives():
    prompts = []
    with _scripted_model([{"items": []}, {"items": ["ok"]}], prompts):
        result = llm.call_llm_json("Do the thing.", "Test Agent", required_keys=["items"], validate=_requires_items)
    assert result == {"items": ["ok"]} and len(prompts) == 2
    assert prompts[0] == "Do the thing."                                   # first try: unchanged
    assert "YOUR PREVIOUS ANSWER WAS REJECTED" in prompts[1] and "items is empty; give at least one" in prompts[1]
    assert prompts[1].startswith("Do the thing.")                          # the original task is kept


def test_malformed_answers_count_as_bad_answers_too():
    prompts = []
    def needs_name(result):
        return result["apps"][0]["name"]                                   # KeyError / TypeError on bad shapes
    with _scripted_model([{"apps": [{}]}, {"apps": ["x"]}, {"apps": [{"name": "a"}]}], prompts):
        result = llm.call_llm_json("p", "Test Agent", required_keys=["apps"], validate=needs_name)
    assert result == {"apps": [{"name": "a"}]} and len(prompts) == 3


def test_it_gives_up_with_a_clear_message_after_the_attempts_run_out():
    prompts = []
    with _scripted_model([{"items": []}] * 3, prompts):
        try:
            llm.call_llm_json("p", "Test Agent", required_keys=["items"], validate=_requires_items)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "Test Agent returned an invalid response after 3 attempts" in str(e) and "items is empty" in str(e)
    assert len(prompts) == 3


def test_a_terse_request_with_no_criteria_first_time_no_longer_kills_the_run():
    # The real failure: "Create a garments website" -> the model returned no acceptance criteria.
    good = {"title": "Garments website", "business_goal": "sell garments", "scope_summary": "A garments website",
            "acceptance_criteria": ["A visitor can reach a website that shows garments"], "out_of_scope": [],
            "build_mode": "from_scratch", "clarifying_questions": []}
    prompts = []
    with _scripted_model([{**good, "acceptance_criteria": []}, good], prompts):
        result = requirement_agent.run("Create a garments website", [])
    assert result["acceptance_criteria"] == good["acceptance_criteria"] and len(prompts) == 2
    assert "acceptance_criteria is empty" in prompts[1] and "do not invent features" in prompts[1]


def test_design_and_release_also_get_a_second_chance():
    prompts = []
    bad_design = _design(change_required=True, new_applications_needed=[], affected_applications=[])
    with _scripted_model([bad_design, _design()], prompts):
        result = design_agent.run({"title": "t"}, [])
    assert result["new_applications_needed"] and "named no affected or new applications" in prompts[1]

    prompts = []
    with _scripted_model([_release([]), {**_release([_story()]), "epic": {"title": "E", "description": "d"},
                                        "rollout_narrative": "n", "monitoring_plan": [], "clarifying_questions": []}], prompts):
        result = release_agent.run({"title": "t"}, _design())
    assert result["stories"] and "no stories" in prompts[1]


@contextmanager
def _jira_env(on=True):
    names = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY")
    saved = {n: os.environ.get(n) for n in names}
    values = ("https://co.atlassian.net", "me@co.com", TOKEN, "ARCH") if on else ("",) * 4
    os.environ.update(dict(zip(names, values)))
    try:
        yield
    finally:
        for n, v in saved.items():
            os.environ[n] = v or ""


@contextmanager
def _fake_jira_in_pipeline(fake):
    original = jira_client.from_env
    jira_client.from_env = lambda **kw: _client(fake)
    try:
        yield
    finally:
        jira_client.from_env = original


def _pipeline_with_jira(fake, **state):
    canned = _scratch_canned("from_scratch")
    canned["Release Agent"]["stories"] = _plan_stories()
    with _jira_env(), _fake_jira_in_pipeline(fake):
        return _run_pipeline([], "Build a todo app from scratch", canned, **state)[0]


def test_pipeline_never_creates_jira_tickets_without_approval():
    # Also: an approval given BEFORE the plan existed is discarded - it cannot
    # authorise a plan nobody had seen yet.
    for state in ({}, {"jira_approved": False}, {"jira_approved": True},
                  {"jira_confirm": lambda preview: False}):
        fake = FakeJira()
        final = _pipeline_with_jira(fake, **state)
        assert fake.calls == [] and final["jira_result"]["status"] == "declined"   # nothing sent
        assert final["run_outcome"] == "APPROVED_RELEASED"                          # local files remain


def test_pipeline_skips_jira_when_it_is_not_configured():
    final, _ = _run_pipeline([], "Build a todo app from scratch", _scratch_canned("from_scratch"),
                             jira_approved=True)              # even "approved" does nothing without settings
    assert final["jira_result"]["status"] == "skipped" and "JIRA_BASE_URL" in final["jira_result"]["reason"]


def test_pipeline_creates_jira_tickets_once_approved_and_asks_with_a_preview():
    asked = []
    fake = FakeJira()
    final = _pipeline_with_jira(fake, jira_confirm=lambda p: asked.append(p) or True)
    assert len(asked) == 1 and asked[0]["stories"] == 2 and TOKEN not in repr(asked[0])
    assert final["jira_result"]["status"] == "created" and final["run_outcome"] == "RELEASED_TO_JIRA"
    assert len(fake.issues) == 5
    assert (config.RUNS_ROOT / final["run_id"] / "jira_mock").is_dir()      # local copy kept too


def test_pipeline_records_a_jira_failure_instead_of_crashing():
    fake = FakeJira(fail_after=1)
    final = _pipeline_with_jira(fake, jira_confirm=lambda p: True)
    assert final["jira_result"]["status"] == "failed" and final["jira_result"]["created_count"] == 1
    assert final["run_outcome"] == "JIRA_FAILED" and final["release_result"]        # local files survive


# When run directly, execute every test_ function and print "ok" for each.
if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
