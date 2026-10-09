"""Requirement Agent (architecture level)."""

from typing import Any, Dict, List, Optional

import llm
from agents._common import format_feedback_section, load_skill, validate_clarifying_questions
from knowledge_base.loader import ApplicationDoc, format_overview_for_prompt

REQUIRED_KEYS = [
    "title",
    "business_goal",
    "scope_summary",
    "acceptance_criteria",
    "out_of_scope",
    "clarifying_questions",
]


def run(
    requirement_text: str,
    applications: List[ApplicationDoc],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
) -> Dict[str, Any]:
    if not requirement_text or not requirement_text.strip():
        raise ValueError("Requirement cannot be empty.")

    prompt = f"""
============================================================
KNOWN APPLICATIONS (overview only -- context, not authoritative for scope)
============================================================

{format_overview_for_prompt(applications)}
{format_feedback_section(revision_feedback, context_notes)}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON:

{{
  "title": "short initiative title",
  "business_goal": "why this matters",
  "scope_summary": "1-3 sentences",
  "acceptance_criteria": ["criterion"],
  "out_of_scope": ["explicit exclusion"],
  "clarifying_questions": [
    {{"question": "...", "why_it_matters": "...", "options": ["...", "..."], "recommended_option": "..."}}
  ]
}}

============================================================
REQUIREMENT
============================================================

{requirement_text}
"""

    result = llm.call_llm_json(
        prompt,
        "Requirement Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("requirement"),
        model=model,
    )

    if not isinstance(result.get("acceptance_criteria"), list) or not result["acceptance_criteria"]:
        raise ValueError("Requirement Agent returned no acceptance criteria.")

    validate_clarifying_questions(result, "Requirement Agent")
    return result
