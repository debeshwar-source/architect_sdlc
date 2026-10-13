"""Checks for the Quality Planner agent and its Excel test plan.
Run: python3 test_quality.py (or pytest)."""

import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm
import orchestrator.graph as graph
import test_hive as t
from agents import quality
from tools import test_plan_doc as tp

try:
    import openpyxl
except ImportError:
    openpyxl = None

REQ = {"title": "Garments Website", "acceptance_criteria": ["A visitor can list garments.", "A visitor can check out."]}


def _case(i, refs, **over):
    case = {"id": f"TC-{i:03d}", "title": f"Case {i}", "requirement_refs": refs, "component": "storefront",
            "type": "Functional", "priority": "High", "preconditions": "Signed in.", "steps": ["Open the page", "Click buy"],
            "test_data": "One shirt", "expected_result": "The order is confirmed."}
    case.update(over)
    return case


PLAN = {"summary": "Cover each criterion.", "test_cases": [_case(1, ["AC-1"]), _case(2, ["AC-2"], type="Negative", priority="Critical")],
        "clarifying_questions": []}


def _fails(plan, criteria=("AC-1", "AC-2")):
    try:
        quality._validate(plan, set(criteria))
    except ValueError as exc:
        return str(exc)
    return None


def test_a_complete_plan_is_accepted():
    assert _fails(PLAN) is None


def test_every_acceptance_criterion_must_be_covered():
    plan = {**PLAN, "test_cases": [_case(1, ["AC-1"])]}
    assert "AC-2" in _fails(plan)
    assert "unknown" in _fails({**PLAN, "test_cases": [_case(1, ["AC-1", "AC-9"]), _case(2, ["AC-2"])]})


def test_bad_cases_are_rejected():
    bad = lambda **over: _fails({**PLAN, "test_cases": [_case(1, ["AC-1", "AC-2"], **over)]})
    assert "invalid type" in bad(type="Magic")
    assert "invalid priority" in bad(priority="Urgent")
    assert "step" in bad(steps=[])
    assert "expected_result" in bad(expected_result=" ")
    assert "duplicate" in _fails({**PLAN, "test_cases": [_case(1, ["AC-1"]), _case(1, ["AC-2"])]})
    assert "no test cases" in _fails({**PLAN, "test_cases": []})


def test_the_agent_sees_the_numbered_criteria_and_returns_a_valid_plan():
    log = []
    original = llm.call_llm_json
    llm.call_llm_json = t._fake_llm({"Quality Planner Agent": PLAN}, log)
    try:
        out = quality.run(REQ, {"change_required": True, "knowledge_base_updates": [{"content": "SECRET-KB-TEXT"}]})
    finally:
        llm.call_llm_json = original
    prompt = log[0][1]
    assert "AC-1: A visitor can list garments." in prompt and "AC-2: A visitor can check out." in prompt
    assert "SECRET-KB-TEXT" not in prompt                                   # knowledge-base drafts are irrelevant to testing
    assert len(out["test_cases"]) == 2


def test_the_pipeline_runs_the_quality_planner_between_design_and_release():
    final, log = t._run_pipeline([], "Build a todo app", t._scratch_canned("from_scratch"))
    order = [a for a, _ in log]
    assert order.index("Design Agent") < order.index("Quality Planner Agent") < order.index("Release Agent")
    assert final["quality_plan"]["test_cases"] and final["run_outcome"] == "APPROVED_RELEASED"
    # stopping after the design skips it; no change needed skips it too
    final, log = t._run_pipeline([], "Build a todo app", t._scratch_canned("from_scratch"), stop_after="design")
    assert "Quality Planner Agent" not in [a for a, _ in log]


def test_a_revised_design_clears_the_old_test_plan():
    original = llm.call_llm_json
    llm.call_llm_json = t._fake_llm({"Design Agent": t._design()})
    try:
        update = graph.design_node({"run_id": graph.new_run_id(), "requirement_text": "t", "requirement": {"title": "t"},
                                    "applications": [], "iteration": 0, "history": [], "quality_plan": PLAN})
    finally:
        llm.call_llm_json = original
    assert update["quality_plan"] == {}


def test_counts_and_coverage():
    assert tp.counts(PLAN) == {"type": {"Functional": 1, "Negative": 1}, "priority": {"Critical": 1, "High": 1}}
    cov = tp.coverage(REQ, PLAN)
    assert [(c["label"], c["cases"]) for c in cov] == [("AC-1", ["TC-001"]), ("AC-2", ["TC-002"])]
    assert tp.coverage(REQ, {"test_cases": [_case(1, ["AC-1"])]})[1]["cases"] == []


def test_the_excel_workbook_is_laid_out_properly():
    if openpyxl is None:
        print("   (openpyxl not installed: workbook not checked)")
        return
    data, name, mime = tp.build(REQ, PLAN, {"run_id": "run-1"})
    assert name == "test-plan-garments-website.xlsx" and "spreadsheetml" in mime
    wb = openpyxl.load_workbook(BytesIO(data))
    assert wb.sheetnames == ["Summary", "Test Cases", "Coverage"]
    ws = wb["Test Cases"]
    header = [c.value for c in ws[1]]
    assert header[:8] == ["ID", "Title", "Component", "Type", "Priority", "Covers", "Preconditions", "Steps"]
    bare = [{k: v for k, v in c.items() if k not in ("preconditions", "test_data")} for c in PLAN["test_cases"]]
    ws_bare = openpyxl.load_workbook(BytesIO(tp.build(REQ, {**PLAN, "test_cases": bare})[0]))["Test Cases"]
    assert [c.value for c in ws_bare[1]][:7] == ["ID", "Title", "Component", "Type", "Priority", "Covers", "Steps"]
    row = {h: c.value for h, c in zip(header, ws[2])}
    assert row["ID"] == "TC-001" and row["Steps"] == "1. Open the page\n2. Click buy" and row["Status"] == "Not run"
    assert row["Covers"] == "AC-1" and row["Expected result"] == "The order is confirmed."
    assert ws.freeze_panes == "C2" and ws.auto_filter.ref and ws.data_validations.dataValidation
    assert [c.value for c in wb["Coverage"][3]][:3] == ["AC-2", "A visitor can check out.", "TC-002"]
    summary = " ".join(str(c.value) for r in wb["Summary"].iter_rows() for c in r if c.value is not None)
    assert "Garments Website — Test Plan" in summary and "Cover each criterion." in summary and "Negative" in summary


def test_odd_input_does_not_break_the_workbook():
    if openpyxl is None:
        return
    odd = {"summary": None, "test_cases": ["junk", {"id": "TC-1", "steps": None, "title": "a\x00b"}]}
    openpyxl.load_workbook(BytesIO(tp.build({}, odd)[0]))
    openpyxl.load_workbook(BytesIO(tp.build(None, {})[0]))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)


def test_the_engineering_manager_sees_the_test_plan_and_the_changes_per_service():
    final, log = t._run_pipeline([], "Build a todo app", t._scratch_canned("from_scratch"))
    prompt = next(p for a, p in log if a == "Release Agent")           # the Engineering Manager (module: release)
    assert "TEST PLAN" in prompt and final["quality_plan"]["test_cases"][0]["id"] in prompt
    assert "todo-api" in prompt                                         # the service whose tickets it must write
