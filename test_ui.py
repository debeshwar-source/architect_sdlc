"""End-to-end checks of the web app (streamlit_app.py), driven the way a person would.
Run: python3 test_ui.py (or pytest). Needs `streamlit` (it uses Streamlit's own test harness).

Plain words: these open the real app, type into it, click its buttons and read what it
shows - with the AI replaced by canned answers and Jira replaced by a fake server, so they
cost nothing and touch nothing real. They cover the parts where a mistake would matter:
nothing goes to Jira or into a knowledge base without a yes, the user can choose which
tickets go and reword them, the run can start and stop where the user chooses, and the
requirement downloads as a Word file or PDF, and can be reworded at its pause.
"""

import importlib.util
import time
from contextlib import contextmanager
from pathlib import Path

import config
import knowledge_base.loader as kb
import llm
import test_hive as t                      # temp folders, blank Jira settings, fake Jira, canned answers
import test_design as d                    # fake Confluence
import tools.confluence_client as confluence_client
import tools.jira_client as jira_client
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent
NEW_KB = "✚ Create something new (a new knowledge base)"
START_AT_DESIGN = "Design agent (skip the Requirement agent)"
DIRECTION = "Use the existing event bus; no new databases."
CREATE = "Create selected tickets in Jira"
SKIP = "Skip — keep local files only"


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def texts(at):
    parts = [c.value for c in at.caption] + [m.value for m in at.markdown] + [i.value for i in at.info]
    parts += [e.value for e in at.error] + [w.value for w in at.warning] + [s.label for s in at.status]
    return " ".join(parts)


def labels(at):
    return [b.label for b in at.button]


def click(at, label):
    for freeze, go in (("Freeze requirements", "Continue to Design"),
                       ("Freeze architecture", "Continue to Quality Planner"),
                       ("Freeze test plan", "Continue to Engineering Manager")):
        if label == "Approve & continue" and freeze in labels(at):
            # At these pauses, approving means freezing and then continuing.
            at.button[labels(at).index(freeze)].click()
            at = at.run()
            label = go
    at.button[labels(at).index(label)].click()
    return at.run()


def select(at, label):
    return [s for s in at.selectbox if s.label == label][0]


def area(at, label):
    return [a for a in at.text_area if a.label == label][0]


def kb_name_box(at):
    """The box for naming a new knowledge base (not the small session-name box in the page's top corner)."""
    return [t for t in at.text_input if not (t.key or "").startswith("session_name_")][0]


def box(at, startswith):
    return [c for c in at.checkbox if c.label.startswith(startswith)][0]


def field(at, kind, value, label=None):
    return [w for w in getattr(at, kind) if w.value == value and (label is None or w.label == label)][0]


def fresh_app():
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=90).run()
    assert not at.exception, at.exception
    return at


def start(text="Add reminders to the todo app", mode="Agentic", choices=None, kb_index=None):
    """Open the app, fill in the form and press the start button."""
    at = fresh_app()
    at.text_area[0].set_value(text)
    at.radio[0].set_value(mode)
    for label, value in (choices or {}).items():
        select(at, label).select(value)
    if kb_index is not None:
        select(at, "Knowledge base").select_index(kb_index)
    at.button[labels(at).index("Draft the blueprint")].click()
    return at.run()


@contextmanager
def scripted_ai(canned):
    """The AI replaced by canned answers; yields the list of (agent, prompt) it was sent."""
    prompts = []
    original = llm.call_llm_json

    def fake(prompt, agent_name, required_keys=None, **kw):
        prompts.append((agent_name, prompt))
        return dict(canned[agent_name]) if agent_name in canned else t._auto_quality_plan(prompt)

    llm.call_llm_json = fake
    try:
        yield prompts
    finally:
        llm.call_llm_json = original


@contextmanager
def fake_jira(fake):
    """Jira configured (with fake credentials) and talking to `fake` instead of the internet."""
    original = jira_client.from_env
    jira_client.from_env = lambda **kw: t._client(fake)
    try:
        with t._jira_env():
            yield fake
    finally:
        jira_client.from_env = original


def jira_canned():
    canned = t._scratch_canned("extend_existing")
    canned["Release Agent"]["stories"] = t._plan_stories()
    return canned


def new_kb_canned():
    canned = t._new_kb_canned()
    canned["Release Agent"]["stories"] = [t._story()]
    return canned


def approve_three_stages(at):
    for _ in range(4):
        assert {"Freeze requirements", "Freeze architecture", "Freeze test plan", "Approve & continue"} & set(labels(at)), labels(at)
        at = click(at, "Approve & continue")
        assert not at.exception, at.exception
    return at


# ------------------------------------------------------------
# Jira: nothing is sent without a yes; the user chooses and rewords
# ------------------------------------------------------------

def test_jira_asks_first_then_creates_only_after_approval():
    with scripted_ai(jira_canned()), fake_jira(t.FakeJira()) as fake:
        at = start()
        assert CREATE in labels(at) and fake.calls == [], "Jira touched before permission"
        assert "Permission needed" in texts(at) and "co.atlassian.net" in texts(at)
        assert t.TOKEN not in texts(at), "token leaked into the page"
        at = click(at, CREATE)
        assert not at.exception, at.exception
        assert "Approved & created in Jira" in texts(at) and len(fake.issues) == 5 and "ARCH-1" in texts(at)


def test_jira_skip_sends_nothing_and_keeps_local_files():
    with scripted_ai(jira_canned()), fake_jira(t.FakeJira()) as fake:
        at = click(start(), SKIP)
        assert not at.exception and fake.calls == []
        assert "saved locally (not sent to Jira)" in texts(at)


def test_jira_failure_can_be_retried_without_duplicates():
    with scripted_ai(jira_canned()), fake_jira(t.FakeJira(fail_after=2)) as fake:
        at = click(start(), CREATE)
        assert not at.exception, at.exception
        assert "creating the Jira tickets failed" in texts(at) and "Retry creating the tickets in Jira" in labels(at)
        assert len(fake.issues) == 2
        fake.fail_after = None
        at = click(at, "Retry creating the tickets in Jira")
        assert not at.exception, at.exception
        assert "Approved & created in Jira" in texts(at) and len(fake.issues) == 5


def test_jira_in_human_mode_comes_after_the_three_stage_approvals():
    with scripted_ai(jira_canned()), fake_jira(t.FakeJira()) as fake:
        at = approve_three_stages(start(mode="Human approval"))
        assert CREATE in labels(at) and fake.calls == []
        at = click(at, CREATE)
        assert "Approved & created in Jira" in texts(at) and len(fake.issues) == 5


def test_jira_not_configured_means_no_prompt_and_local_files_only():
    fake = t.FakeJira()
    with scripted_ai(jira_canned()):
        at = start()
    assert not at.exception, at.exception
    assert CREATE not in labels(at) and fake.calls == []
    assert "saved locally (not sent to Jira)" in texts(at) and "not configured" in texts(at)


def test_the_user_chooses_which_tickets_go_to_jira():
    with scripted_ai(jira_canned()), fake_jira(t.FakeJira()) as fake:
        at = start()
        assert box(at, "Epic:").value and box(at, "Story S1").value and box(at, "Second").value
        assert "1 epic, 2 of 2 stories, 2 of 2 sub-tasks" in texts(at)
        box(at, "Second").uncheck()
        at = at.run()
        assert "1 epic, 1 of 2 stories, 1 of 2 sub-tasks" in texts(at), "counts did not update"
        sub = [c for c in at.checkbox if c.label.startswith("Sub-task")]
        assert sub[1].proto.disabled and not sub[0].proto.disabled      # a sub-task needs its story
        assert fake.calls == []
        at = click(at, CREATE)
        assert not at.exception, at.exception
        assert len(fake.issues) == 3
        assert "Not sent to Jira (not selected): 1 stories, 1 sub-tasks" in texts(at)


def test_select_none_blocks_create_and_a_story_can_go_without_an_epic():
    with scripted_ai(jira_canned()), fake_jira(t.FakeJira()) as fake:
        at = click(start(), "Select none")
        assert [b for b in at.button if b.label == CREATE][0].proto.disabled
        assert fake.calls == []
        box(at, "Story S1").check()
        at = at.run()
        assert "1 of 2 stories" in texts(at) and "without one" in texts(at)
        assert "0 of 2 sub-tasks" in texts(at)                        # ticking a story does not tick its sub-tasks
        [c for c in at.checkbox if c.label.startswith("Sub-task")][0].check()
        at = click(at.run(), CREATE)
        assert not at.exception, at.exception
        story = next(f for f in fake.issues.values() if f["issuetype"] == {"id": "2"})
        assert len(fake.issues) == 2 and "parent" not in story          # story + one sub-task, no epic
        at2 = click(click(start(), "Select none"), "Select all")
        assert "1 epic, 2 of 2 stories, 2 of 2 sub-tasks" in texts(at2)


def _local_markdown(since):
    return "\n".join(f.read_text() for f in config.RUNS_ROOT.glob("*/jira_mock/*.md") if f.stat().st_mtime > since)


def test_reworded_tickets_reach_jira_and_the_local_files():
    since = time.time() - 1
    with scripted_ai(jira_canned()), fake_jira(t.FakeJira()) as fake:
        at = start()
        field(at, "text_input", "Story S1", "Story title").set_value("Reworded S1")
        field(at, "text_area", "write code", "Steps inside this sub-task (one per line)").set_value("write code\nreview code")
        at = at.run()
        assert "2 wording edit(s) will be saved" in texts(at)
        assert fake.calls == [], "nothing may be sent while editing"
        at = click(at, CREATE)
        assert not at.exception, at.exception
        story = next(f for f in fake.issues.values() if f["issuetype"] == {"id": "2"} and "Reworded" in f["summary"])
        assert story["summary"] == "Reworded S1"
        assert any("review code" in f["description"] for f in fake.issues.values() if f["issuetype"] == {"id": "4"})
        assert not any(f["summary"] == "Story S1" for f in fake.issues.values())
        md = _local_markdown(since)
        assert "Reworded S1" in md and "review code" in md, "local files must match what was sent"
        assert "Reworded S1" in texts(at) and "Approved & created in Jira" in texts(at)


def test_a_blank_title_is_refused_but_skip_still_works():
    since = time.time() - 1
    with scripted_ai(jira_canned()), fake_jira(t.FakeJira()) as fake:
        at = start()
        field(at, "text_input", "Story S1", "Story title").set_value("   ")
        at = at.run()
        assert any("Every ticket needs a title" in e.value for e in at.error)
        assert [b for b in at.button if b.label == CREATE][0].proto.disabled
        at = click(at, SKIP)
        assert not at.exception and fake.calls == []
        assert "Story S1" in _local_markdown(since)                    # the bad edit was discarded


def test_reword_then_skip_updates_the_local_files_only():
    since = time.time() - 1
    with scripted_ai(jira_canned()), fake_jira(t.FakeJira()) as fake:
        at = start()
        field(at, "text_input", "Story S1", "Story title").set_value("Kept locally")
        at = click(at.run(), SKIP)
        assert not at.exception and fake.calls == []
        assert "Kept locally" in _local_markdown(since)


# ------------------------------------------------------------
# Knowledge bases: chosen at the start, saved only after a yes at the end
# ------------------------------------------------------------

def test_with_no_knowledge_bases_the_new_one_is_named_and_saved_only_on_a_yes():
    with t._kb_root() as root, scripted_ai(new_kb_canned()):
        at = fresh_app()
        assert select(at, "Knowledge base").options == [NEW_KB]
        assert any("No knowledge bases yet" in i.value for i in at.info)
        at = start(text="Build a todo app")
        assert not at.exception, at.exception
        assert "Save knowledge base" in labels(at) and "Don't save" in labels(at)
        assert kb.list_knowledge_bases() == [], "written before the user answered!"
        assert kb_name_box(at).value == "todo-app"                    # suggested from the project title
        kb_name_box(at).set_value("Todo KB!")
        at = at.run()
        assert "Will be saved as `todo-kb`" in texts(at)
        at = click(at, "Save knowledge base")
        assert not at.exception, at.exception
        assert [k.name for k in kb.list_knowledge_bases()] == ["todo-kb"] and (root / "todo-kb.md").is_file()
        assert "Saved to new knowledge base" in texts(at)


def test_a_taken_name_is_blocked_and_dont_save_writes_nothing():
    with t._kb_root() as root, scripted_ai(new_kb_canned()):
        existing = kb.create_knowledge_base("todo-kb")
        kb.create_doc("todo-api", "# todo-api\n\n- **Purpose:** keeps todos", existing)
        options = select(fresh_app(), "Knowledge base").options
        assert len(options) == 2 and options[0].startswith("todo-kb (1 applications)") and "Create something new" in options[1]
        at = start(text="Build a todo app", kb_index=1)                  # create something new
        kb_name_box(at).set_value("todo-kb")
        at = at.run()
        assert "already exists" in texts(at)
        assert [b for b in at.button if b.label == "Save knowledge base"][0].proto.disabled
        kb_name_box(at).set_value("another")
        at = click(at.run(), "Don't save")
        assert [k.name for k in kb.list_knowledge_bases()] == ["todo-kb"]
        assert "not saved" in texts(at).lower()


def test_a_selected_knowledge_base_is_only_appended_to_after_approval():
    with t._kb_root() as root:
        existing = kb.create_knowledge_base("todo-kb")
        kb.create_doc("todo-api", "# todo-api\n\n- **Purpose:** keeps todos", existing)
        canned = t._scratch_canned("extend_existing")
        canned["Design Agent"] = t._design(
            affected_applications=[{"name": "todo-api", "role_in_change": "r", "reason": "r"}],
            new_applications_needed=[], dependency_order=["todo-api"],
            knowledge_base_updates=[{"application": "todo-api", "action": "augment",
                                     "content": "- now sends reminders", "reason": "r"}])
        canned["Release Agent"]["stories"] = [t._reading(t._story(), "todo-api")]
        before = existing.read_text()
        with scripted_ai(canned):
            at = start(text="Build a todo app", kb_index=0)
        assert not at.exception, at.exception
        assert "Add these to the knowledge base" in texts(at) and "todo-kb" in texts(at)
        assert existing.read_text() == before, "changed before approval!"
        at = click(at, "Save knowledge base")
        assert not at.exception, at.exception
        after = existing.read_text()
        assert after.startswith(before.rstrip()) and "now sends reminders" in after       # appended at the end, nothing rewritten
        assert [k.name for k in kb.list_knowledge_bases()] == ["todo-kb"]


def test_the_knowledge_base_question_comes_after_the_stage_approvals_in_human_mode():
    with t._kb_root(), scripted_ai(new_kb_canned()):
        at = approve_three_stages(start(text="Build a todo app", mode="Human approval"))
        assert "Save knowledge base" in labels(at)
        kb_name_box(at).set_value("human-run-kb")
        at = click(at.run(), "Save knowledge base")
        assert [k.name for k in kb.list_knowledge_bases()] == ["human-run-kb"]


# ------------------------------------------------------------
# Which agents run, and architectural direction in between
# ------------------------------------------------------------

def _heads(at):
    return [b.label for b in at.button if (b.key or "").endswith("_head") and "dd_" in (b.key or "")]


def test_the_page_has_a_small_wordmark_and_no_large_heading():
    at = fresh_app()
    assert not at.title, "no big heading"
    assert any('class="app-name"' in m.value and "SDLC Studio" in m.value for m in at.markdown)


def test_the_landing_page_shows_only_the_start_and_no_agents_yet():
    at = fresh_app()
    names = [h.split("  ", 1)[1] for h in _heads(at)]
    assert all(h.startswith("▸") for h in _heads(at)), "every card starts closed"
    assert names[0] == "➕ New requirement" and names[1].startswith("📚 Knowledge bases")
    assert not [n for n in names if any(a in n for a in ("Engineering Manager 2", "Coding agent", "Code reviewer", "QA agent"))], names   # none of the later agents yet
    assert not [s for s in at.subheader if s.value.startswith("Start")]
    css = " ".join(m.value for m in at.markdown)
    assert ".st-key-dd_new_body{display:none}" in css                                              # the form is out of sight until asked for
    at.button(key="dd_new_head").click().run()
    assert at.button(key="dd_new_head").label == "▾  ➕ New requirement"
    assert ".st-key-dd_new_body{display:none}" not in " ".join(m.value for m in at.markdown)       # now it shows


def test_each_later_agent_appears_only_after_the_step_before_it(monkeypatch):
    import progress

    def names(**flags):
        monkeypatch.setattr(progress, "flags", lambda run_id, has_plan: {"em2": False, "review": False, "qa": False, **flags})
        return " | ".join(h.split("  ", 1)[1] for h in _heads(fresh_app()))
    assert "Engineering Manager 2" not in names() and "Code reviewer" not in names() and "QA agent" not in names()
    after_plan = names(em2=True)
    assert "Engineering Manager 2" in after_plan and "Code reviewer" not in after_plan and "QA agent" not in after_plan
    after_pr = names(em2=True, review=True)
    assert "Code reviewer" in after_pr and "QA agent" not in after_pr
    after_merge = names(em2=True, review=True, qa=True)
    assert after_merge.index("Engineering Manager 2") < after_merge.index("Code reviewer") < after_merge.index("QA agent")


def test_a_session_can_be_saved_under_a_custom_name_and_a_new_run_starts_unnamed():
    import session_store
    with scripted_ai(jira_canned()):
        at = start(mode="Agentic")
    assert not at.exception, at.exception
    rid = at.session_state["run_id"]
    assert any(b.label == "Save" and b.key == f"session_name_save_{rid}" for b in at.button)         # the control is there once a session is open
    at.text_input(key=f"session_name_{rid}").set_value("  ").run()
    at.button(key=f"session_name_save_{rid}").click().run()
    assert any("Type a name" in e.value for e in at.error) and "session_name" not in at.session_state     # nothing saved for a blank name
    at.text_input(key=f"session_name_{rid}").set_value("Reminders rollout v1").run()
    at.button(key=f"session_name_save_{rid}").click().run()
    assert not at.exception, at.exception
    assert any("Saved as “Reminders rollout v1”" in s.value for s in at.success)
    assert session_store.read(rid)["session"]["session_name"] == "Reminders rollout v1"                 # on disk, so it survives closing the app
    item = next(s for s in session_store.list_sessions() if s["run_id"] == rid)
    assert item["title"] == "Reminders rollout v1" and item["named"]
    at2 = fresh_app()
    assert not [b for b in at2.button if (b.key or "").startswith("session_name_save_")]                 # no session open: nothing to name


def test_the_new_requirement_card_tucks_itself_away_once_a_run_starts():
    at = fresh_app()
    at.button(key="dd_new_head").click().run()
    at.text_area[0].set_value("Add reminders to the todo app")
    at.radio[0].set_value("Agentic")
    with scripted_ai(jira_canned()):
        at.button[labels(at).index("Draft the blueprint")].click()
        at = at.run()
    assert not at.exception, at.exception
    assert at.button(key="dd_new_head").label == "▸  ➕ New requirement"


def test_the_form_has_only_a_start_with_choice():
    at = fresh_app()
    assert select(at, "Start with").value == "Requirement agent"
    assert select(at, "Start with").options == ["Requirement agent", START_AT_DESIGN]
    assert not [s for s in at.selectbox if s.label == "Stop after"]
    assert not [a for a in at.text_area if a.label.startswith("Architectural direction")]
    assert [a.label for a in at.text_area][0] == "Requirement"            # the only box before the run is the request


def test_the_requirement_agent_can_be_skipped():
    with t._kb_root(), scripted_ai(new_kb_canned()) as prompts:
        at = start(text="Build a todo app", choices={"Start with": START_AT_DESIGN})
        assert not at.exception, at.exception
        assert {a for a, _ in prompts} == {"Design Agent", "Quality Planner Agent", "Release Agent"}
        assert "used as written (agent skipped)" in texts(at)
        assert "Build a todo app" in [p for a, p in prompts if a == "Design Agent"][0]
        assert "Save knowledge base" in labels(at)                      # the design's documents are still offered
        assert "saved locally (not sent to Jira)" in texts(click(at, "Don't save"))


def test_skipping_the_requirement_agent_removes_the_pointless_pause():
    with t._kb_root(), scripted_ai(new_kb_canned()) as prompts:
        at = start(text="Build a todo app", mode="Human approval", choices={"Start with": START_AT_DESIGN})
        assert {a for a, _ in prompts} == {"Design Agent"}, prompts       # the first pause is after Design
        assert "Freeze architecture" in labels(at)       # the first pause is the design's


# ------------------------------------------------------------
# The requirement as a document
# ------------------------------------------------------------

def _requirement_canned():
    canned = t._scratch_canned("from_scratch")
    canned["Requirement Agent"].update(
        title="Garments Website", business_goal="Sell garments online.", scope_summary="A public garments site.",
        acceptance_criteria=["A visitor can list garments.", "A visitor can check out."], out_of_scope=["Mobile app"],
        clarifying_questions=[{"question": "Take payments directly?", "why_it_matters": "Changes cost.",
                               "options": ["Redirect to a provider", "Take cards"],
                               "recommended_option": "Redirect to a provider"}])
    return canned


def _have_document_libraries():
    try:
        import docx, reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def test_the_requirement_is_shown_as_a_document_with_word_and_pdf_downloads():
    with scripted_ai(_requirement_canned()):
        at = start(text="Create a garments website", mode="Human approval")      # the document is shown at the pause
    assert not at.exception, at.exception
    page = "\n".join(m.value for m in at.markdown)
    assert "# Garments Website" in page and "## Acceptance criteria" in page
    assert "**AC-1** A visitor can list garments." in page and "## Open questions" not in page
    assert "Business goal" not in page and "Sell garments online." not in page      # not part of the document
    assert "Download this requirement as a document" not in texts(at)       # only after the requirements are frozen
    assert list(at.get("download_button")) == []
    at = click(at, "Freeze requirements")
    assert not at.exception, at.exception
    assert "Download this requirement as a document" in texts(at) and "Continue to Design" in labels(at)
    buttons = list(at.get("download_button"))
    if _have_document_libraries():
        assert sorted(b.proto.label for b in buttons) == ["Download PDF", "Download Word document (.docx)"]
    else:
        assert buttons == [] and "pip install python-docx" in texts(at) and "pip install reportlab" in texts(at)


def test_no_document_is_offered_when_the_requirement_agent_was_skipped():
    with t._kb_root(), scripted_ai(new_kb_canned()):
        at = start(text="Build a todo app", choices={"Start with": START_AT_DESIGN})
    assert not at.exception, at.exception
    assert not [b for b in at.get("download_button") if "requirement" in str(b.proto.label).lower()]
    assert "Download this requirement" not in texts(at)


def test_the_document_is_visible_at_the_pause_in_human_mode():
    with scripted_ai(_requirement_canned()):
        at = start(text="Create a garments website", mode="Human approval")
    assert not at.exception, at.exception
    assert "# Garments Website" in "\n".join(m.value for m in at.markdown) and "Freeze requirements" in labels(at)


# ------------------------------------------------------------
# Rewording the requirement
# ------------------------------------------------------------

EDIT_EXPANDER = "Edit the wording of this requirement"


def _req_field(at, label):
    return [w for w in list(at.text_input) + list(at.text_area) if w.label == label][0]


def _approve_enabled(at):
    return not [b for b in at.button if b.label == "Freeze requirements"][0].proto.disabled


def _pipeline_requirement(at):
    return at.session_state["pipeline_state"]["requirement"]


def test_the_requirement_can_be_reworded_at_its_pause_and_the_design_uses_the_new_wording():
    with t._kb_root(), scripted_ai(_requirement_canned()) as prompts:
        at = start(text="Create a garments website", mode="Human approval")
        assert any(EDIT_EXPANDER in e.label for e in at.expander), "no editor at the pause"
        assert _req_field(at, "Title").value == "Garments Website"
        _req_field(at, "Title").set_value("Online Garments Store")
        _req_field(at, "Scope").set_value("A public store where visitors browse and order garments.")
        _req_field(at, "Acceptance criteria (one per line)").set_value("Visitors can browse garments.\nVisitors can check out.\nOrders are emailed.")
        at = at.run()
        assert "3 wording edit(s)" in texts(at)
        assert _pipeline_requirement(at)["title"] == "Garments Website", "must not change until saved/approved"
        at = click(at, "Approve & continue")
        assert not at.exception, at.exception
        assert _pipeline_requirement(at)["title"] == "Online Garments Store"
        design_prompt = [p for a, p in prompts if a == "Design Agent"][0]
        assert "A public store where visitors browse and order garments." in design_prompt
        assert "Orders are emailed." in design_prompt and "Garments Website" not in design_prompt.split("OUTPUT")[0].split("REQUIREMENT")[1].split("FULL KNOWLEDGE BASE")[0]
        page = "\n".join(m.value for m in at.markdown)
        assert "# Online Garments Store" in page and "**AC-3** Orders are emailed." in page   # the document shows it too


def test_save_updates_the_document_straight_away_and_nothing_else_changes():
    with t._kb_root(), scripted_ai(_requirement_canned()) as prompts:
        at = start(text="Create a garments website", mode="Human approval")
        _req_field(at, "Title").set_value("Renamed")
        at = click(at.run(), "Save wording changes")
        assert not at.exception, at.exception
        assert "# Renamed" in "\n".join(m.value for m in at.markdown)       # the document refreshed
        assert _pipeline_requirement(at)["title"] == "Renamed"
        assert "Freeze requirements" in labels(at) and {a for a, _ in prompts} == {"Requirement Agent"}  # still paused
        assert _pipeline_requirement(at)["business_goal"] == "Sell garments online."                  # untouched


def test_an_invalid_rewording_blocks_approval_until_fixed():
    with t._kb_root(), scripted_ai(_requirement_canned()) as prompts:
        at = start(text="Create a garments website", mode="Human approval")
        _req_field(at, "Title").set_value("   ")
        _req_field(at, "Acceptance criteria (one per line)").set_value("")
        at = at.run()
        assert any("The requirement needs a title, at least one acceptance criterion" in e.value for e in at.error)
        assert not _approve_enabled(at) and "Fix the problem" in texts(at)
        assert [b for b in at.button if b.label == "Save wording changes"][0].proto.disabled
        _req_field(at, "Title").set_value("Fixed title")
        _req_field(at, "Acceptance criteria (one per line)").set_value("One criterion.")
        at = at.run()
        assert _approve_enabled(at)
        at = click(at, "Approve & continue")
        assert _pipeline_requirement(at)["acceptance_criteria"] == ["One criterion."] and "Design Agent" in {a for a, _ in prompts}


def test_no_editor_once_the_design_has_been_made_from_the_requirement():
    with t._kb_root(), scripted_ai(new_kb_canned()):
        at = start(text="Build a todo app")                                # a full unattended run
        assert not at.exception, at.exception
        assert not any(EDIT_EXPANDER in e.label for e in at.expander)
        assert "Save wording changes" not in labels(at)
        at2 = approve_three_stages(start(text="Build a todo app", mode="Human approval"))   # and after the pause
        assert not any(EDIT_EXPANDER in e.label for e in at2.expander)


# When run directly, execute every test_ function and print "ok" for each.
# ------------------------------------------------------------
# Design document, revising the design, publishing to Confluence
# ------------------------------------------------------------

@contextmanager
def fake_confluence(fake):
    """Confluence configured (fake credentials) and talking to `fake` instead of the internet."""
    original = confluence_client.from_env
    confluence_client.from_env = lambda **kw: d.client(fake)
    try:
        with d.confluence_env():
            yield fake
    finally:
        confluence_client.from_env = original


def test_the_design_is_shown_as_a_document_with_diagrams_and_downloads():
    with scripted_ai(jira_canned()):
        at = start(mode="Human approval")
        at = click(at, "Approve & continue")                    # requirement approved; now at the design pause
    assert not at.exception, at.exception
    assert len(at.get("image")) >= 1, "the architecture diagram is not shown"
    assert "Revise the design" in labels(at) and "Freeze architecture" in labels(at)
    assert "Tell the Design agent what to change (optional)" in [a.label for a in at.text_area]


def test_revising_the_design_sends_the_note_to_the_design_agent():
    with scripted_ai(jira_canned()) as prompts:
        at = start(mode="Human approval")
        at = click(at, "Approve & continue")
        area(at, "Tell the Design agent what to change (optional)").set_value("Put a queue between orders and email.")
        at = click(at, "Revise the design")
    assert not at.exception, at.exception
    design_prompts = [p for a, p in prompts if a == "Design Agent"]
    assert len(design_prompts) == 2 and "Put a queue between orders and email." in design_prompts[-1]


def test_confluence_asks_first_then_publishes_only_after_a_yes():
    fake = d.FakeConfluence()
    with fake_confluence(fake), scripted_ai(jira_canned()):
        at = start()
        assert "Publish to Confluence" in labels(at) and "Skip — don't publish" in labels(at)
        assert not fake.pages and not [c for c in fake.calls if c[0] in ("POST", "PUT")]
        at = click(at, "Publish to Confluence")
    assert not at.exception, at.exception
    assert len(fake.pages) == 1 and list(fake.attachments[next(iter(fake.pages))])


def test_confluence_skip_publishes_nothing():
    fake = d.FakeConfluence()
    with fake_confluence(fake), scripted_ai(jira_canned()):
        at = click(start(), "Skip — don't publish")
    assert not at.exception, at.exception
    assert not fake.pages and "Publish to Confluence" not in labels(at)


def test_confluence_not_configured_means_no_prompt():
    with d.confluence_env(on=False), scripted_ai(jira_canned()):
        at = start()
    assert not at.exception, at.exception
    assert "Publish to Confluence" not in labels(at)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_in_the_desktop_window_a_download_saves_in_place(tmp_path, monkeypatch):
    # In the desktop window a download link would navigate the window to the file; instead the file
    # is saved to Downloads and the person stays on the page.
    if not _have_document_libraries():
        return
    monkeypatch.setenv("BLUEPRINT_PARENT_PID", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    with scripted_ai(_requirement_canned()):
        at = start(text="Create a garments website", mode="Human approval")
        at = click(at, "Freeze requirements")
        assert list(at.get("download_button")) == [] and "Download PDF" in labels(at)
        at = click(at, "Download PDF")
    assert not at.exception, at.exception
    saved = list((tmp_path / "Downloads").glob("*.pdf"))
    assert len(saved) == 1 and saved[0].read_bytes().startswith(b"%PDF")
    assert "Saved to" in texts(at) and "Continue to Design" in labels(at)       # still on the page


def test_freezing_the_architecture_offers_downloads_and_confluence():
    fake = d.FakeConfluence()
    with fake_confluence(fake), scripted_ai(jira_canned()):
        at = start(mode="Human approval")
        at = click(at, "Approve & continue")                        # requirement frozen and approved -> design pause
        assert "Publish to Confluence" not in labels(at) and not [b for b in at.get("download_button") if "design" in b.proto.label]   # a draft only
        at = click(at, "Freeze architecture")
        assert not at.exception, at.exception
        if _have_document_libraries():
            assert len([b for b in at.get("download_button") if "design" in b.proto.label]) == 2
        assert "Publish to Confluence" in labels(at) and "Continue to Quality Planner" in labels(at)
        assert not fake.pages                                       # nothing sent until asked
        at = click(at, "Publish to Confluence")
        assert not at.exception, at.exception
        assert len(fake.pages) == 1 and "Published:" in texts(at)
        at = click(at, "Continue to Quality Planner")
        assert not at.exception, at.exception
        at = click(at, "Approve & continue")                        # quality planning
        at = click(at, "Approve & continue")                        # release planning
        for _ in range(4):                                          # Jira / knowledge base questions, if any
            if "Publish to Confluence" in labels(at):
                break
            for label in ("Skip", SKIP, "Don't save"):
                if label in labels(at):
                    at = click(at, label)
                    break
    assert "Publish to Confluence" not in labels(at), "asked again at the end after being published at the freeze"
    assert len(fake.pages) == 1


def test_continuing_without_publishing_does_not_ask_again_at_the_end():
    fake = d.FakeConfluence()
    with fake_confluence(fake), scripted_ai(jira_canned()):
        at = start(mode="Human approval")
        at = click(at, "Approve & continue")
        at = click(at, "Approve & continue")                        # freeze architecture, then continue
        at = click(at, "Approve & continue")                        # quality planning
        at = click(at, "Approve & continue")                        # release planning
    assert not at.exception, at.exception
    assert not fake.pages and "Publish to Confluence" not in labels(at)


def test_the_quality_planner_pauses_after_the_design_and_offers_an_excel_download(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUEPRINT_PARENT_PID", "1")                 # desktop mode: the file is saved in place
    monkeypatch.setenv("HOME", str(tmp_path))
    with scripted_ai(jira_canned()) as prompts:
        at = start(mode="Human approval")
        at = click(at, "Approve & continue")                        # requirement
        at = click(at, "Approve & continue")                        # design
        assert not at.exception, at.exception
        assert "Quality Planner Agent" in {a for a, _ in prompts} and "Release Agent" not in {a for a, _ in prompts}
        assert "Freeze test plan" in labels(at) and "Reconsider" in labels(at)
        assert "Download Excel workbook (.xlsx)" not in labels(at)  # a draft until frozen
        at = click(at, "Freeze test plan")
        assert not at.exception, at.exception
        assert "Continue to Engineering Manager" in labels(at)
        if _have_document_libraries() and importlib.util.find_spec("openpyxl"):
            at = click(at, "Download Excel workbook (.xlsx)")
            saved = list((tmp_path / "Downloads").glob("*.xlsx"))
            assert len(saved) == 1 and saved[0].read_bytes()[:2] == b"PK"
        at = click(at, "Continue to Engineering Manager")
        assert not at.exception, at.exception
        assert "Release Agent" in {a for a, _ in prompts}
