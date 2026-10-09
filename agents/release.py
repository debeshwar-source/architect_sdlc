"""Release & Operations Agent (architecture level) -- epic + tickets."""

from typing import Any, Dict, Optional

import llm
from agents._common import (
    format_dict,
    format_feedback_section,
    load_skill,
    validate_clarifying_questions,
)

REQUIRED_KEYS = [
    "epic",
    "tickets",
    "rollout_stages",
    "rollout_narrative",
    "monitoring_plan",
    "clarifying_questions",
]


def run(
    requirement: Dict[str, Any],
    architecture: Dict[str, Any],
    review: Dict[str, Any],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
) -> Dict[str, Any]:
    affected = architecture.get("affected_applications", [])
    new_apps = architecture.get("new_applications_needed", [])
    expected_names = {a["name"] for a in affected} | {a["name"] for a in new_apps}

    prompt = f"""
============================================================
REQUIREMENT
============================================================

{format_dict(requirement)}

============================================================
ARCHITECTURE PLAN (dependency_order is authoritative -- do not reorder)
============================================================

{format_dict(architecture)}

============================================================
REVIEW OUTCOME
============================================================

{format_dict(review)}

============================================================
APPLICATIONS REQUIRING A TICKET
============================================================

{chr(10).join(f"- {name}" for name in sorted(expected_names))}

Produce exactly one ticket per application listed above.
{format_feedback_section(revision_feedback, context_notes)}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON matching the contract in your skill.
"""

    result = llm.call_llm_json(
        prompt,
        "Release Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("release"),
        model=model,
    )

    _validate(result, expected_names)
    validate_clarifying_questions(result, "Release Agent")
    return result


VALID_PRIORITIES = {"Highest", "High", "Medium", "Low", "Lowest"}
VALID_STORY_POINTS = {1, 2, 3, 5, 8, 13}


def _validate(result: Dict[str, Any], expected_names: set) -> None:
    if not isinstance(result.get("rollout_stages"), list) or not result["rollout_stages"]:
        raise ValueError("Release Agent returned no rollout stages.")

    tickets = result.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        raise ValueError("Release Agent returned no tickets.")

    for ticket in tickets:
        app = ticket.get("application")
        if ticket.get("priority") not in VALID_PRIORITIES:
            raise ValueError(
                f"Release Agent ticket for {app} has invalid priority "
                f"{ticket.get('priority')!r}; must be one of {sorted(VALID_PRIORITIES)}."
            )
        if ticket.get("story_points") not in VALID_STORY_POINTS:
            raise ValueError(
                f"Release Agent ticket for {app} has invalid story_points "
                f"{ticket.get('story_points')!r}; must be one of {sorted(VALID_STORY_POINTS)}."
            )
        if not isinstance(ticket.get("labels"), list) or not ticket["labels"]:
            raise ValueError(f"Release Agent ticket for {app} has no labels.")

    actual_names = {t.get("application") for t in tickets}

    missing = expected_names - actual_names
    if missing:
        raise ValueError(f"Release Agent is missing ticket(s) for: {missing}")

    unexpected = actual_names - expected_names
    if unexpected:
        raise ValueError(
            f"Release Agent produced ticket(s) for application(s) not in "
            f"scope: {unexpected}"
        )

    for ticket in tickets:
        blocked_by = set(ticket.get("blocked_by", []))
        unknown_blockers = blocked_by - expected_names
        if unknown_blockers:
            raise ValueError(
                f"Ticket for {ticket.get('application')} is blocked_by "
                f"application(s) not in this release: {unknown_blockers}"
            )
