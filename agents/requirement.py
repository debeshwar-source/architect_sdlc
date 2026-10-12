"""Requirement Agent (architecture level).

Plain words: the FIRST stage. It takes the user's raw request written in
everyday language and turns it into a tidy, structured statement: a
title, the business goal, what is and isn't included, and how we'll know
it's done (acceptance criteria).
"""

from typing import Any, Dict, List, Optional

import llm
from agents._common import format_feedback_section, load_skill, validate_clarifying_questions
from knowledge_base.loader import SCRATCH_KB_NOTICE, ApplicationDoc, format_overview_for_prompt

# The fields the AI's answer must contain, or the answer is rejected.
REQUIRED_KEYS = [
    "title",
    "business_goal",
    "scope_summary",
    "acceptance_criteria",
    "out_of_scope",
    "build_mode",
    "clarifying_questions",
]

# "from_scratch" = a brand-new build that must not use any existing system;
# "extend_existing" = a change to, or integration with, what already exists.
BUILD_MODES = {"from_scratch", "extend_existing"}


def _check(result: Dict[str, Any]) -> None:
    """Content checks on the Requirement agent's answer (raises ValueError)."""
    # There must be at least one acceptance criterion, otherwise nobody could
    # tell when the work is "done". A brief request still needs one: a minimal,
    # checkable statement that follows directly from the request's own words.
    if not isinstance(result.get("acceptance_criteria"), list) or not result["acceptance_criteria"]:
        raise ValueError(
            "acceptance_criteria is empty. Every requirement needs at least one testable "
            "criterion. If the request is brief, give the minimal criteria that follow directly "
            "from its own words (do not invent features), and put anything uncertain in "
            "clarifying_questions instead of leaving this list empty."
        )
    # The build mode must be one of the two known values.
    if result.get("build_mode") not in BUILD_MODES:
        raise ValueError(f"build_mode must be 'from_scratch' or 'extend_existing', not {result.get('build_mode')!r}.")


def run(
    requirement_text: str,
    applications: List[ApplicationDoc],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
    from_scratch: bool = False,
) -> Dict[str, Any]:
    # `from_scratch=True` means the user explicitly asked for a from-scratch
    # build: the existing applications are then not shown to the AI at all.
    # Stop early if the user gave us nothing to work with.
    if not requirement_text or not requirement_text.strip():
        raise ValueError("Requirement cannot be empty.")

    # Build the message sent to the AI: a short overview of the existing
    # applications (for context), any reviewer feedback, the exact JSON
    # shape we want back, and finally the user's requirement itself.
    # (The doubled braces {{ }} just print a literal { } inside an f-string.)
    prompt = f"""
============================================================
KNOWN APPLICATIONS (overview only -- context, not authoritative for scope)
============================================================

{SCRATCH_KB_NOTICE if from_scratch else format_overview_for_prompt(applications)}
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
  "build_mode": "from_scratch or extend_existing",
  "clarifying_questions": [
    {{"question": "...", "why_it_matters": "...", "options": ["...", "..."], "recommended_option": "..."}}
  ]
}}

============================================================
REQUIREMENT
============================================================

{requirement_text}
"""

    # Send the prompt to the AI. `required_keys` lists the fields the
    # answer must contain; `system` is this agent's instruction sheet
    # (skills/requirement.md); `model` optionally picks a specific AI model.
    result = llm.call_llm_json(
        prompt,
        "Requirement Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("requirement"),
        model=model,
        validate=_check,      # a bad answer is sent back for correction, not fatal
    )

    # Final guard (the retry above normally makes this a no-op).
    _check(result)
    # If the user forced a from-scratch build, that choice always wins over
    # the AI's own judgment.
    if from_scratch:
        result["build_mode"] = "from_scratch"

    # Tidy the optional questions the AI wants to ask a human.
    validate_clarifying_questions(result, "Requirement Agent")
    return result
