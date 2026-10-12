"""Design Agent -- the core planning role (formerly the "Architecture Agent").

Plain words: the second stage and the "brain" of the plan. It reads the
requirement and decides how to build it:
  - Is any change needed at all?
  - Which existing applications must change, and in what order?
  - Are brand-new applications required?
  - How will it be tested, and what could go wrong?

The knowledge base (descriptions of the existing applications) is OPTIONAL.
  - With a knowledge base, the agent grounds its plan in those documents.
  - With none (an empty or missing folder), it designs from scratch: every
    application it needs is proposed as a NEW application.
Either way it can return `knowledge_base_updates`: new documents to create,
or additions to append to existing ones, so the knowledge base grows as the
system is designed. (The pipeline applies them at the final step - see
orchestrator/graph.py - this agent only proposes them.)
"""

from typing import Any, Dict, List, Optional

import llm
from agents._common import (
    format_dict,
    format_feedback_section,
    load_skill,
    validate_clarifying_questions,
)
from knowledge_base.loader import (
    SCRATCH_KB_NOTICE,
    ApplicationDoc,
    application_names,
    format_full_kb_for_prompt,
)

# Fields the AI's answer must contain, or the answer is rejected.
REQUIRED_KEYS = [
    "change_required",
    "affected_applications",
    "unaffected_applications_considered",
    "new_applications_needed",
    "cross_service_contracts",
    "dependency_order",
    "test_strategy",
    "architecture_risks",
    "knowledge_base_updates",
    "clarifying_questions",
]

# The only two things a knowledge-base update may do.
VALID_KB_ACTIONS = {"create", "augment"}


def run(
    requirement: Dict[str, Any],
    applications: List[ApplicationDoc],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
    from_scratch: bool = False,
) -> Dict[str, Any]:
    # FROM-SCRATCH BUILD: the existing knowledge base must play no part. It
    # is removed here, in the agent itself, so this holds no matter what the
    # caller passes in: the AI is never shown it, and the validation below
    # (which checks names against `applications`) then rejects any answer
    # that mentions an existing application.
    if from_scratch:
        applications = []

    # Reviewer feedback / background notes (empty on a first attempt).
    feedback_section = format_feedback_section(revision_feedback, context_notes)

    # Message to the AI: the requirement, the full knowledge base (or a
    # note that there is none), any feedback, and the output instructions.
    prompt = f"""
============================================================
REQUIREMENT
============================================================

{format_dict(requirement)}

============================================================
FULL KNOWLEDGE BASE (every known application)
============================================================

{SCRATCH_KB_NOTICE if from_scratch else format_full_kb_for_prompt(applications)}
{feedback_section}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON matching the contract in your skill.
"""

    # Send the prompt to the AI. `required_keys` lists the fields the
    # answer must contain; `system` is this agent's instruction sheet
    # (skills/design.md); `model` optionally picks a specific AI model.
    result = llm.call_llm_json(
        prompt,
        "Design Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("design"),
        model=model,
        validate=lambda r: _validate(r, application_names(applications)),   # bad answer -> corrected, not fatal
    )

    # Double-check the AI's answer makes logical sense (see _validate).
    _validate(result, application_names(applications))
    _clean_kb_updates(result)
    validate_clarifying_questions(result, "Design Agent")
    return result


def _validate(result: Dict[str, Any], known_apps: List[str]) -> None:
    """Reject self-contradicting answers (raises ValueError, which makes
    the AI call be retried)."""
    # "change_required" must be strictly true/false.
    if not isinstance(result["change_required"], bool):
        raise ValueError("Design Agent 'change_required' must be a boolean.")

    # Collect the names of applications to change and of new ones to build.
    affected_names = {a["name"] for a in result["affected_applications"]}
    new_names = {a["name"] for a in result["new_applications_needed"]}

    # Every application named must either exist already or be explicitly
    # proposed as new - no invented applications. (With an empty knowledge
    # base nothing "exists", so everything must be proposed as new.)
    unknown = affected_names - set(known_apps) - new_names
    if unknown:
        raise ValueError(
            f"Design Agent referenced unknown application(s) not in "
            f"the knowledge base and not proposed as new: {unknown}"
        )

    # Says "change needed" but names nothing to change -> contradiction.
    if result["change_required"] and not affected_names and not new_names:
        raise ValueError(
            "Design Agent requires a change but named no affected "
            "or new applications."
        )

    # Says "no change needed" but lists things to change -> contradiction.
    if not result["change_required"] and (affected_names or new_names):
        raise ValueError(
            "Design Agent named applications to change despite "
            "change_required being false."
        )

    # The build order may only mention applications that are part of the plan.
    dependency_order = result["dependency_order"]
    all_names = affected_names | new_names
    order_names = set(dependency_order)
    if order_names - all_names:
        raise ValueError(
            f"dependency_order references application(s) not in "
            f"affected_applications/new_applications_needed: "
            f"{order_names - all_names}"
        )


def _clean_kb_updates(result: Dict[str, Any]) -> None:
    """Tidy the proposed knowledge-base updates, in place. Like the
    clarifying questions, this is a best-effort extra: a malformed entry
    is dropped rather than failing the whole run over it."""
    updates = result.get("knowledge_base_updates")
    # No change needed -> nothing worth recording in the knowledge base.
    if not result.get("change_required") or not isinstance(updates, list):
        result["knowledge_base_updates"] = []
        return

    cleaned = []
    for u in updates:
        # Keep only well-formed entries: a name, a known action, and
        # actual content to write.
        if (
            isinstance(u, dict)
            and str(u.get("application") or "").strip()
            and u.get("action") in VALID_KB_ACTIONS
            and str(u.get("content") or "").strip()
        ):
            cleaned.append(
                {
                    "application": str(u["application"]).strip(),
                    "action": u["action"],
                    "content": str(u["content"]),
                    "reason": str(u.get("reason") or ""),
                }
            )
    result["knowledge_base_updates"] = cleaned
