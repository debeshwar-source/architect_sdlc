"""Tests for task assignment (agents/catalog.py), its use by the Engineering Manager, and the bounce limits."""

import copy
from pathlib import Path

import pytest

import config
import knowledge_base.loader as kb
import test_hive as t
import token_log
from agents import catalog, escalation
from agents import release as release_agent
from orchestrator import graph
from tools import issue_tracker


def doc(name, headings=("Purpose", "Depends on", "Events")):
    body = f"# {name}\n\n**Purpose:** does {name}.\n\n" + "\n\n".join(f"## {h}\n\ntext" for h in headings)
    return kb.ApplicationDoc(name, Path(name), body, None, f"does {name}")


def a(**over):
    return t._assignment(**over)


def problems(assignment, apps=None):
    return catalog.check_assignment(assignment, "S1/T1", apps)


# ---------- the catalog and the checks ----------

def test_a_valid_assignment_passes():
    assert problems(a()) == []


def test_unknown_agents_and_skills_are_rejected():
    assert "not one of" in problems(a(agent="coding_cobol"))[0]
    assert "has no skill" in problems(a(skills=["time-series"]))[0]              # each agent is one skill file now: no separate skills
    assert problems(a(skills=[])) == []                                          # empty = the agent's whole skill


def test_the_output_must_say_what_is_delivered_and_how_done_is_checked():
    bad = a(); bad["output"]["done_checks"] = []
    assert any("done_checks" in p for p in problems(bad))
    bad = a(); bad["output"]["branch"] = ""
    assert any("needs branch" in p for p in problems(bad))
    bad = a(); bad["output"]["paths"] = []
    assert any("needs paths" in p for p in problems(bad))
    bad = a(); bad["output"]["type"] = "essay"
    assert any("output.type" in p for p in problems(bad))


def test_files_that_belong_to_another_agent_are_caught():
    swift = a(); swift["output"]["paths"] = ["App/Views/Home.swift"]
    assert any("do not belong to coding_python" in p for p in problems(swift))
    folder = a(); folder["output"]["paths"] = ["src/orders/"]                    # no extension: nothing to contradict
    assert problems(folder) == []


def test_decisions_belong_to_people_and_needs_human_explains_itself():
    d = a(); d["output"]["type"] = "decision"
    assert any("made by a person" in p for p in problems(d))
    h = a(agent="needs_human", skills=[]); h["output"] = {"type": "decision", "summary": "Pick a vendor", "done_checks": ["vendor chosen"]}
    assert any("reason" in p for p in problems(h))
    h["reason"] = "business choice"
    assert problems(h) == []


def test_knowledge_base_references_must_exist_and_headings_must_match():
    apps = [doc("todo-api"), doc("billing")]
    ok = a(kb_refs=[{"doc": "todo-api", "sections": ["Depends on"]}])
    assert problems(ok, apps) == []
    assert any("not in the knowledge base index" in p for p in problems(a(kb_refs=[{"doc": "nope", "sections": []}]), apps))
    assert any("no heading like" in p for p in problems(a(kb_refs=[{"doc": "todo-api", "sections": ["Pricing"]}]), apps))
    assert problems(a(kb_refs=[{"doc": "todo-api", "sections": ["depends on (calls)"]}]), apps) == []   # lenient match
    assert problems(a(kb_refs=[{"doc": "todo-api", "sections": []}]), apps) == []                    # whole document


def test_a_story_must_read_its_own_documented_application_somewhere():
    apps = [doc("todo-api")]
    story = t._story()
    assert any("no task references" in p for p in catalog.check_plan([story], apps))
    story["tasks"][0]["assignment"] = a(kb_refs=[{"doc": "todo-api", "sections": []}])
    assert catalog.check_plan([story], apps) == []
    assert catalog.check_plan([t._story()], []) == []                               # new build: nothing to reference


def test_all_problems_come_back_together_so_one_retry_fixes_them():
    s = t._story(); s["tasks"][0]["assignment"] = a(agent="x")
    s2 = t._story("S2"); s2["tasks"][0]["assignment"] = a(skills=["time-series"])
    errs = catalog.check_plan([s, s2], [])
    assert len(errs) == 2


# ---------- the Engineering Manager ----------

def test_the_release_validator_requires_assignments_only_when_asked():
    plain = t._story(); del plain["tasks"][0]["assignment"]
    release_agent._validate(t._release([plain]), {"todo-api"})                      # old behaviour, still fine
    with pytest.raises(ValueError, match="assignment"):
        release_agent._validate(t._release([plain]), {"todo-api"}, [], True)


def test_a_bad_assignment_is_sent_back_once_with_every_problem_and_then_accepted():
    bad = _release_with(agent="coding_cobol")
    good = _release_with()
    prompts = []
    with t._scripted_model([bad, good], prompts):
        out = release_agent.run({"title": "t"}, _design_for("todo-api"), applications=[])
    assert len(prompts) == 2 and "Task assignments need fixing" in prompts[1]
    assert out["assignment_summary"]["tasks_per_agent"] == {"coding_python": 1}


def _design_for(*names):
    return {"affected_applications": [], "new_applications_needed": [{"name": n} for n in names]}


def _release_with(**over):
    story = t._story(); story["tasks"][0]["assignment"] = a(**over)
    return {"epic": {"title": "E", "description": "d"}, "stories": [story], "rollout_stages": [{"stage": 1}],
            "rollout_narrative": "n", "monitoring_plan": [], "clarifying_questions": []}


def test_the_manager_prompt_carries_only_a_short_agent_list_and_a_headings_only_index():
    apps = [doc("todo-api"), doc("billing"), doc("other-" + "x" * 3)]
    for d in apps:
        d.content += "\n\nBODY-TEXT-THAT-MUST-NOT-APPEAR " * 400
    prompts = []
    ok = _release_with(kb_refs=[{"doc": "todo-api", "sections": ["Events"]}])
    with t._scripted_model([ok], prompts):
        release_agent.run({"title": "t"}, {"affected_applications": [{"name": "todo-api"}], "new_applications_needed": []},
                          applications=apps)
    p = prompts[0]
    assert "AGENTS YOU CAN ASSIGN" in p and "coding_data_science" in p and "KNOWLEDGE BASE INDEX" in p
    assert "- todo-api: does todo-api | sections: todo-api | Purpose | Depends on | Events" in p
    assert "BODY-TEXT-THAT-MUST-NOT-APPEAR" not in p
    assert token_log.est_tokens(catalog.prompt_block()) < 450


def test_a_new_build_tells_the_manager_there_is_nothing_to_reference():
    assert "new build" in catalog.kb_index([], {"x"})


# ---------- tickets, edits, Jira ----------

def test_tickets_show_the_assignment(tmp_path):
    story = t._story(); story["tasks"][0]["assignment"] = a(kb_refs=[{"doc": "todo-api", "sections": ["Events"]}])
    issue_tracker.write_release(tmp_path, {"title": "E", "description": "d"}, [story], "run1")
    page = next(p for p in tmp_path.glob("*-T1.md")).read_text()
    assert "## Assignment" in page and "Agent: coding_python" in page and "branch feature/todo-endpoint" in page
    assert "todo-api (Events)" in page


def test_a_reviewer_can_change_the_agent_but_only_to_a_valid_assignment():
    story = t._story()
    plan = {"epic": {"title": "E", "description": "d"}, "stories": [story]}
    state = {"run_id": "r1", "release_plan": plan, "applications": []}
    edited = copy.deepcopy(plan)
    edited["stories"][0]["tasks"][0]["assignment"]["agent"] = "coding_web"          # web cannot own a .py file
    with pytest.raises(ValueError, match="do not belong to coding_web"):
        graph.apply_ticket_edits(state, edited)
    edited["stories"][0]["tasks"][0]["assignment"] = a(agent="coding_data_engineering", skills=[])
    update = graph.apply_ticket_edits(state, edited)
    assert update["release_plan"]["stories"][0]["tasks"][0]["assignment"]["agent"] == "coding_data_engineering"


def test_the_jira_subtask_description_carries_the_assignment():
    assert "Agent: coding_python" in "\n".join(catalog.assignment_lines(a()))
    import inspect
    from tools import jira_client
    assert "h3. Assignment" in inspect.getsource(jira_client)


# ---------- bounce limits ----------

def test_a_task_can_only_bounce_a_few_times(monkeypatch):
    monkeypatch.setattr(config, "ESC_BOUNCES_PER_TASK", 2)
    led = escalation.BounceLedger()
    assert led.allow("S1/T1", "Which agent owns the Kotlin shared module?", "manager")
    assert led.allow("S1/T1", "Is the payments contract section needed?", "manager")
    assert not led.allow("S1/T1", "Should the branch be named differently?", "manager")      # third


def test_the_same_question_is_never_asked_twice_and_answers_are_remembered():
    led = escalation.BounceLedger()
    assert led.allow("S1/T1", "Which agent owns the shared module?", "manager")
    assert not led.allow("S1/T1", "which agent owns the shared module", "manager")            # near-identical
    led.record_answer("Which agent owns the shared module?", "coding_android")
    assert led.known_answer("which agent owns the shared module") == "coding_android"


def test_questions_only_move_up_and_the_user_is_asked_sparingly(monkeypatch):
    monkeypatch.setattr(config, "ESC_USER_QUESTIONS_PER_RELEASE", 1)
    led = escalation.BounceLedger()
    assert not led.allow("S1/T1", "Back down to the agent?", "agent", from_="manager")
    assert led.allow("S1/T1", "Budget for the vendor option?", "user", from_="manager")
    assert not led.allow("S2/T1", "Which launch date?", "user", from_="manager")             # release-wide cap


def test_reassigning_is_limited_and_the_total_extra_calls_are_capped(monkeypatch):
    led = escalation.BounceLedger()
    assert led.allow_reassign("S1/T1") and not led.allow_reassign("S1/T1")
    monkeypatch.setattr(config, "ESC_EXTRA_CALLS_PER_RELEASE", 1)
    assert led.allow("S1/T2", "first question about x?", "manager")
    assert not led.allow("S2/T1", "totally different question?", "manager")


def test_the_ledger_survives_a_resume_and_a_limit_closes_the_task_out_without_failing():
    led = escalation.BounceLedger()
    led.allow("S1/T1", "A question?", "manager")
    again = escalation.BounceLedger(led.to_dict())
    assert again.bounces == {"S1/T1": 1}
    closed = escalation.close_out(a(), "Which agent?", "bounces_per_task")
    assert closed["needs_review"] and closed["open_question"] == "Which agent?" and "bounces_per_task" in closed["assumption"]
    story = t._story(); story["tasks"][0]["assignment"] = closed
    assert catalog.summarise([story])["needs_review"] == ["S1/T1"]


def test_a_task_flagged_for_review_says_so_in_its_ticket():
    closed = escalation.close_out(a(), "Which agent owns the shared module?", "bounces_per_task")
    assert any(l.startswith("NEEDS REVIEW - open question: Which agent") for l in catalog.assignment_lines(closed))
