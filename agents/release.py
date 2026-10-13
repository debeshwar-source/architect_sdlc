"""Engineering Manager Agent (module name: release) -- the final stage: the Jira epic, stories, tasks and subtasks.

Plain words: once the design and the test plan are settled, this agent acts as the
engineering manager. It looks at what changes in each service and turns that into a
ready-to-file Jira backlog:

    Epic            the whole initiative
     └─ Story       a deliverable slice of work (usually one per application)
         └─ Task    a concrete piece of engineering work inside the story
             └─ Subtask   a small, checkable step inside the task

It also produces a staged rollout plan (what goes live first, how to know
it is safe to continue, how to roll back) and a monitoring plan.
"""

from typing import Any, Dict, Optional

import llm
from agents._common import (
    format_dict,
    format_feedback_section,
    load_skill,
    validate_clarifying_questions,
)

# Fields the AI's answer must contain.
REQUIRED_KEYS = [
    "epic",
    "stories",
    "rollout_stages",
    "rollout_narrative",
    "monitoring_plan",
    "clarifying_questions",
]


def run(
    requirement: Dict[str, Any],
    design: Dict[str, Any],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
    quality_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Applications that must each be covered by at least one story: those
    # being changed plus any brand-new ones.
    affected = design.get("affected_applications", [])
    new_apps = design.get("new_applications_needed", [])
    expected_names = {a["name"] for a in affected} | {a["name"] for a in new_apps}

    # The proposed knowledge-base documents can be long and are not needed
    # to plan tickets, so leave them out of this agent's view of the design.
    design_for_prompt = {k: v for k, v in design.items() if k != "knowledge_base_updates"}

    # The test plan, in short: the engineering work for each service should account for it.
    test_cases = [
        {k: c.get(k) for k in ("id", "title", "component", "type", "priority", "requirement_refs")}
        for c in (quality_plan or {}).get("test_cases", [])
    ]
    test_plan_text = format_dict({"summary": (quality_plan or {}).get("summary", ""), "test_cases": test_cases}) \
        if test_cases else "(no test plan was made)"

    # Message to the AI: requirement, the design (build order is final),
    # and the list of applications the backlog must cover.
    # (chr(10) is a newline character; used to put one name per line.)
    prompt = f"""
============================================================
REQUIREMENT
============================================================

{format_dict(requirement)}

============================================================
DESIGN (dependency_order is authoritative -- do not reorder)
============================================================

{format_dict(design_for_prompt)}

============================================================
TEST PLAN (test cases the tickets must make room for, per service)
============================================================

{test_plan_text}

============================================================
APPLICATIONS THE BACKLOG MUST COVER
============================================================

{chr(10).join(f"- {name}" for name in sorted(expected_names))}

Every application listed above must be the `component` of at least one
story.
{format_feedback_section(revision_feedback, context_notes)}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON matching the contract in your skill.
"""

    # Send the prompt to the AI (instruction sheet: skills/release.md).
    result = llm.call_llm_json(
        prompt,
        "Release Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("release"),
        model=model,
        validate=lambda r: _validate(r, expected_names),   # bad answer -> corrected, not fatal
    )

    # Check the backlog is complete and consistent.
    _validate(result, expected_names)
    validate_clarifying_questions(result, "Release Agent")
    return result


# Allowed values, mirroring a typical Jira setup.
VALID_PRIORITIES = {"Highest", "High", "Medium", "Low", "Lowest"}
# Story points = effort estimate, on the common Fibonacci-style scale.
VALID_STORY_POINTS = {1, 2, 3, 5, 8, 13}


def _validate(result: Dict[str, Any], expected_names: set) -> None:
    """Reject incomplete or inconsistent release plans (raises ValueError,
    which causes the AI call to be retried)."""
    # There must be at least one rollout stage.
    if not isinstance(result.get("rollout_stages"), list) or not result["rollout_stages"]:
        raise ValueError("Release Agent returned no rollout stages.")

    # There must be at least one story.
    stories = result.get("stories")
    if not isinstance(stories, list) or not stories:
        raise ValueError("Release Agent returned no stories.")

    # Every story needs a unique id, a valid priority, valid story points,
    # labels, and at least one task - each task at least one subtask.
    ids = set()
    for story in stories:
        sid = story.get("id")
        if not sid or sid in ids:
            raise ValueError(f"Release Agent story has a missing or duplicate id: {sid!r}")
        ids.add(sid)

        if story.get("priority") not in VALID_PRIORITIES:
            raise ValueError(
                f"Release Agent story {sid} has invalid priority "
                f"{story.get('priority')!r}; must be one of {sorted(VALID_PRIORITIES)}."
            )
        if story.get("story_points") not in VALID_STORY_POINTS:
            raise ValueError(
                f"Release Agent story {sid} has invalid story_points "
                f"{story.get('story_points')!r}; must be one of {sorted(VALID_STORY_POINTS)}."
            )
        if not isinstance(story.get("labels"), list) or not story["labels"]:
            raise ValueError(f"Release Agent story {sid} has no labels.")

        tasks = story.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError(f"Release Agent story {sid} has no tasks.")
        for task in tasks:
            if not str(task.get("title") or "").strip():
                raise ValueError(f"Release Agent story {sid} has a task with no title.")
            subtasks = task.get("subtasks")
            if not isinstance(subtasks, list) or not subtasks:
                raise ValueError(
                    f"Release Agent task {task.get('title')!r} in story {sid} has no subtasks."
                )
            for sub in subtasks:
                if not str(sub.get("title") or "").strip():
                    raise ValueError(
                        f"Release Agent task {task.get('title')!r} has a subtask with no title."
                    )

    # Each story's component must be an application that is part of the
    # plan, and every application in the plan must be covered.
    components = {s.get("component") for s in stories}
    unexpected = components - expected_names
    if unexpected:
        raise ValueError(
            f"Release Agent produced story(ies) for component(s) not in "
            f"scope: {unexpected}"
        )
    missing = expected_names - components
    if missing:
        raise ValueError(f"Release Agent has no story covering: {missing}")

    # "blocked_by" (waiting on another story) may only name stories that
    # exist in this release, and a story cannot block itself.
    for story in stories:
        blocked_by = set(story.get("blocked_by", []))
        unknown_blockers = blocked_by - ids
        if unknown_blockers or story["id"] in blocked_by:
            raise ValueError(
                f"Story {story['id']} has invalid blocked_by "
                f"(unknown or self): {unknown_blockers or {story['id']}}"
            )
