"""Quality Agent (architecture level) -- cross-service test strategy."""

from typing import Any, Dict, Optional

import llm
from agents._common import (
    format_dict,
    format_feedback_section,
    load_skill,
    validate_clarifying_questions,
)

REQUIRED_KEYS = [
    "test_strategy_summary",
    "contract_tests",
    "per_application_test_scenarios",
    "end_to_end_scenarios",
    "clarifying_questions",
]


def run(
    requirement: Dict[str, Any],
    architecture: Dict[str, Any],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
) -> Dict[str, Any]:
    prompt = f"""
============================================================
REQUIREMENT
============================================================

{format_dict(requirement)}

============================================================
ARCHITECTURE PLAN
============================================================

{format_dict(architecture)}
{format_feedback_section(revision_feedback, context_notes)}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON matching the contract in your skill.
"""

    result = llm.call_llm_json(
        prompt,
        "Quality Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("quality"),
        model=model,
    )
    validate_clarifying_questions(result, "Quality Agent")
    return result
