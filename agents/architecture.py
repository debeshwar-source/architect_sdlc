"""Architecture Agent (architecture level) -- the core planning role."""

from typing import Any, Dict, List, Optional

import llm
from agents._common import (
    format_dict,
    format_feedback_section,
    load_skill,
    validate_clarifying_questions,
)
from knowledge_base.loader import ApplicationDoc, application_names, format_full_kb_for_prompt

REQUIRED_KEYS = [
    "change_required",
    "affected_applications",
    "unaffected_applications_considered",
    "new_applications_needed",
    "cross_service_contracts",
    "dependency_order",
    "architecture_risks",
    "clarifying_questions",
]


def run(
    requirement: Dict[str, Any],
    applications: List[ApplicationDoc],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
) -> Dict[str, Any]:
    feedback_section = format_feedback_section(revision_feedback, context_notes)

    prompt = f"""
============================================================
REQUIREMENT
============================================================

{format_dict(requirement)}

============================================================
FULL KNOWLEDGE BASE (every known application)
============================================================

{format_full_kb_for_prompt(applications)}
{feedback_section}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON matching the contract in your skill.
"""

    result = llm.call_llm_json(
        prompt,
        "Architecture Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("architecture"),
        model=model,
    )

    _validate(result, application_names(applications))
    validate_clarifying_questions(result, "Architecture Agent")
    return result


def _validate(result: Dict[str, Any], known_apps: List[str]) -> None:
    if not isinstance(result["change_required"], bool):
        raise ValueError("Architecture Agent 'change_required' must be a boolean.")

    affected_names = {a["name"] for a in result["affected_applications"]}
    new_names = {a["name"] for a in result["new_applications_needed"]}

    unknown = affected_names - set(known_apps) - new_names
    if unknown:
        raise ValueError(
            f"Architecture Agent referenced unknown application(s) not in "
            f"the knowledge base and not proposed as new: {unknown}"
        )

    if result["change_required"] and not affected_names and not new_names:
        raise ValueError(
            "Architecture Agent requires a change but named no affected "
            "or new applications."
        )

    if not result["change_required"] and (affected_names or new_names):
        raise ValueError(
            "Architecture Agent named applications to change despite "
            "change_required being false."
        )

    dependency_order = result["dependency_order"]
    all_names = affected_names | new_names
    order_names = set(dependency_order)
    if order_names - all_names:
        raise ValueError(
            f"dependency_order references application(s) not in "
            f"affected_applications/new_applications_needed: "
            f"{order_names - all_names}"
        )
