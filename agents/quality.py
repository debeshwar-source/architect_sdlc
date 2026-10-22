"""Quality Planner Agent -- the stage after Design: a test plan in plain English.

Plain words: once the design is settled, this agent writes the test cases a
tester would carry out -- not unit tests or code, but the planning view: what
to check, how, and what a correct result looks like. Each case points back to
the acceptance criteria it proves (AC-1, AC-2, ...), and every criterion must
be covered by at least one case. The result is turned into an Excel workbook
by tools/test_plan_doc.py.
"""

from typing import Any, Dict, Optional

import llm
from agents._common import (
    format_dict,
    format_feedback_section,
    load_skill,
    validate_clarifying_questions,
)

REQUIRED_KEYS = ["summary", "test_cases", "clarifying_questions"]

CASE_TYPES = ["Functional", "Integration", "End-to-end", "Negative", "Edge case",
              "Security", "Performance", "Usability", "Regression", "Data"]
PRIORITIES = ["Critical", "High", "Medium", "Low"]


def criteria_ids(requirement: Dict[str, Any]) -> Dict[str, str]:
    """The requirement's acceptance criteria labelled AC-1, AC-2, ... (the same labels the
    requirement document shows)."""
    items = [str(c).strip() for c in (requirement or {}).get("acceptance_criteria") or [] if str(c).strip()]
    return {f"AC-{i}": text for i, text in enumerate(items, start=1)}


def run(
    requirement: Dict[str, Any],
    design: Dict[str, Any],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
) -> Dict[str, Any]:
    criteria = criteria_ids(requirement)
    # The knowledge-base documents the design proposed are long and irrelevant to testing.
    design_for_prompt = {k: v for k, v in design.items() if k != "knowledge_base_updates"}

    prompt = f"""
============================================================
REQUIREMENT
============================================================

{format_dict(requirement)}

============================================================
ACCEPTANCE CRITERIA (refer to them by these labels; each needs at least one test case)
============================================================

{chr(10).join(f"{label}: {text}" for label, text in criteria.items()) or "(none were stated)"}

============================================================
DESIGN
============================================================

{format_dict(design_for_prompt)}
{format_feedback_section(revision_feedback, context_notes)}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON matching the contract in your skill.
"""

    result = llm.call_llm_json(
        prompt, "Quality Planner Agent",
        system=load_skill("quality"),
        required_keys=REQUIRED_KEYS,
        model=model,
        validate=lambda r: _validate(r, set(criteria)),     # a bad answer is corrected, not fatal
    )
    _validate(result, set(criteria))
    validate_clarifying_questions(result, "Quality Planner Agent")
    return result


def _validate(result: Dict[str, Any], expected_criteria: set) -> None:
    """Reject an incomplete or inconsistent test plan (raises ValueError, which makes the AI
    call be retried with the message)."""
    cases = result.get("test_cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Quality Planner Agent returned no test cases.")

    ids, covered = set(), set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Every test case must be an object.")
        cid = str(case.get("id") or "").strip()
        if not cid or cid in ids:
            raise ValueError(f"A test case has a missing or duplicate id: {cid!r}")
        ids.add(cid)
        for field in ("title", "expected_result", "component"):
            if not str(case.get(field) or "").strip():
                raise ValueError(f"Test case {cid} has no {field}.")
        if case.get("type") not in CASE_TYPES:
            raise ValueError(f"Test case {cid} has invalid type {case.get('type')!r}; must be one of {CASE_TYPES}.")
        if case.get("priority") not in PRIORITIES:
            raise ValueError(f"Test case {cid} has invalid priority {case.get('priority')!r}; must be one of {PRIORITIES}.")
        steps = case.get("steps")
        if not isinstance(steps, list) or not steps or any(not str(s).strip() for s in steps):
            raise ValueError(f"Test case {cid} needs at least one step, and no blank ones.")
        refs = case.get("requirement_refs")
        if not isinstance(refs, list):
            raise ValueError(f"Test case {cid}: requirement_refs must be a list (it may be empty).")
        unknown = {str(r) for r in refs} - expected_criteria
        if unknown:
            raise ValueError(f"Test case {cid} refers to unknown acceptance criteria: {sorted(unknown)}.")
        covered |= {str(r) for r in refs}

    missing = sorted(expected_criteria - covered, key=lambda label: int(label.split("-")[1]))
    if missing:
        raise ValueError(f"No test case covers: {', '.join(missing)}. Every acceptance criterion needs at least one.")
