"""
Implementation Spec Agent (architecture level "Coding" role).

Produces one detailed, self-contained spec per affected application.
Each spec is deliberately written to be usable as-is as the --spec
input to the granular ai_native_sdlc engine for that application's
repo -- this is the seam between the two systems, even though this
system never invokes that one directly.
"""

from typing import Any, Dict, List, Optional

import llm
from agents._common import (
    format_dict,
    format_feedback_section,
    load_skill,
    validate_clarifying_questions,
)

REQUIRED_KEYS = ["specs", "clarifying_questions"]


def run(
    requirement: Dict[str, Any],
    architecture: Dict[str, Any],
    quality: Dict[str, Any],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
) -> Dict[str, Any]:
    affected = architecture.get("affected_applications", [])
    new_apps = architecture.get("new_applications_needed", [])
    expected_names = {a["name"] for a in affected} | {a["name"] for a in new_apps}

    if not expected_names:
        raise ValueError(
            "Implementation Spec Agent invoked with no affected/new applications."
        )

    prompt = f"""
============================================================
REQUIREMENT
============================================================

{format_dict(requirement)}

============================================================
ARCHITECTURE PLAN
============================================================

{format_dict(architecture)}

============================================================
TEST STRATEGY
============================================================

{format_dict(quality)}

============================================================
APPLICATIONS REQUIRING A SPEC
============================================================

{chr(10).join(f"- {name}" for name in sorted(expected_names))}

Produce exactly one spec per application listed above, no more, no
fewer.
{format_feedback_section(revision_feedback, context_notes)}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON matching the contract in your skill.
"""

    result = llm.call_llm_json(
        prompt,
        "Implementation Spec Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("implementation_spec"),
        model=model,
    )

    _validate(result, expected_names)
    validate_clarifying_questions(result, "Implementation Spec Agent")
    return result


def _validate(result: Dict[str, Any], expected_names: set) -> None:
    specs = result.get("specs")
    if not isinstance(specs, list) or not specs:
        raise ValueError("Implementation Spec Agent returned no specs.")

    for spec in specs:
        _default_work_order(spec)

    actual_names = {spec.get("application") for spec in specs}

    missing = expected_names - actual_names
    if missing:
        raise ValueError(f"Implementation Spec Agent is missing spec(s) for: {missing}")

    unexpected = actual_names - expected_names
    if unexpected:
        raise ValueError(
            f"Implementation Spec Agent produced spec(s) for application(s) "
            f"not in scope: {unexpected}"
        )


def _default_work_order(spec: Dict[str, Any]) -> None:
    """objective/boundaries/definition_of_done make each spec a work
    order (see the skill). A thin answer must not fail the run -- fill
    any gap with a safe default so every spec carries all three."""
    if not str(spec.get("objective") or "").strip():
        spec["objective"] = spec.get("summary") or f"Implement this application's part of the change."
    if not spec.get("boundaries"):
        spec["boundaries"] = ["Implement only this application's role_in_change; no scope creep."]
    if not spec.get("definition_of_done"):
        spec["definition_of_done"] = [
            "This application's test scenarios and contract tests from the quality plan pass."
        ]
