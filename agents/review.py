"""Review Agent (architecture level) -- reviews the plan itself."""

from typing import Any, Dict, List, Optional

import llm
from agents._common import (
    format_dict,
    format_feedback_section,
    load_skill,
    validate_clarifying_questions,
)
from knowledge_base.loader import ApplicationDoc, format_full_kb_for_prompt

REQUIRED_KEYS = ["status", "summary", "findings", "clarifying_questions"]


def run(
    requirement: Dict[str, Any],
    architecture: Dict[str, Any],
    quality: Dict[str, Any],
    implementation_spec: Dict[str, Any],
    applications: List[ApplicationDoc],
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

============================================================
TEST STRATEGY
============================================================

{format_dict(quality)}

============================================================
IMPLEMENTATION SPECS
============================================================

{format_dict(implementation_spec)}

============================================================
KNOWLEDGE BASE (to check constraints against)
============================================================

{format_full_kb_for_prompt(applications)}
{format_feedback_section(revision_feedback, context_notes)}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON matching the contract in your skill.
"""

    result = llm.call_llm_json(
        prompt,
        "Review Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("review"),
        model=model,
    )

    _validate(result)
    validate_clarifying_questions(result, "Review Agent")
    return result


def _validate(result: Dict[str, Any]) -> None:
    if result.get("status") not in {"PASS", "CHANGES_REQUESTED"}:
        raise ValueError(f"Review Agent returned invalid status: {result.get('status')}")

    findings = result.get("findings", [])
    has_blocking = any(f.get("severity") == "BLOCKING" for f in findings)

    # The orchestrator's invariant is authoritative over the LLM's own
    # summary, in both directions -- same pattern as the granular
    # engine's review agent, and for the same reason: a model that
    # mislabels a confirmation as BLOCKING (or vice versa) shouldn't
    # be trusted over the findings it actually produced.
    result["status"] = "CHANGES_REQUESTED" if has_blocking else "PASS"
