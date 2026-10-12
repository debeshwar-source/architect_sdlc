"""
Discussion Assistant (architecture level) -- advisory chat, not a
pipeline stage.

Unlike every other module in agents/, this one never produces
structured output and never touches pipeline state: it's the
free-form "talk it through" counterpart to the Answer/Override
controls at a human-approval gate, called directly from the
Streamlit UI rather than from orchestrator/graph.py. It has no
`clarifying_questions` of its own and nothing it returns is ever fed
back into another agent's prompt.

Plain words: a chat helper. When a human is asked to approve a stage, they
may want to talk it over first ("why did you choose this?"). This module
answers in ordinary conversational text. It is advice only - it never
changes the plan.
"""

import json
from typing import Any, Dict, List, Optional

import llm
from agents._common import load_skill
from knowledge_base.loader import SCRATCH_KB_NOTICE, ApplicationDoc, format_full_kb_for_prompt

# (pipeline_state key, section label) -- in pipeline order. Unlike
# agents/_common.py's format_dict(), this deliberately does NOT strip
# clarifying_questions: those are exactly the kind of thing a human
# would want to discuss, and nothing here ever feeds another agent.
STAGE_RESULT_LABELS = [
    ("requirement", "REQUIREMENT"),
    ("design", "DESIGN"),
    ("release_plan", "RELEASE PLAN"),
]
# Each pair means: look up the stage's result under this key and print it
# under this heading.


def _format_context(pipeline_state: Dict[str, Any], applications: List[ApplicationDoc]) -> str:
    """Bundle the knowledge base and every finished stage's result into
    one block of text so the assistant knows the full situation."""
    # A from-scratch build passes no applications: say so explicitly rather
    # than implying an (empty) knowledge base exists.
    sections = [
        "============================================================",
        "KNOWLEDGE BASE",
        "============================================================",
        "",
        format_full_kb_for_prompt(applications) if applications else SCRATCH_KB_NOTICE,
    ]
    for key, label in STAGE_RESULT_LABELS:
        value = pipeline_state.get(key)
        # Skip stages that haven't run yet.
        if not value:
            continue
        sections += [
            "",
            "============================================================",
            label,
            "============================================================",
            "",
            json.dumps(value, indent=2, default=str),
        ]
    return "\n".join(sections)


def _format_transcript(messages: List[Dict[str, str]]) -> str:
    """Write the chat so far as plain lines: "HUMAN: ..." / "YOU: ..."."""
    return "\n".join(
        f"{'HUMAN' if m['role'] == 'user' else 'YOU'}: {m['content']}" for m in messages
    )


def reply(
    messages: List[Dict[str, str]],
    pipeline_state: Dict[str, Any],
    pending_stage: str,
    model: Optional[str] = None,
) -> str:
    """Return the assistant's reply to the human's latest chat message."""
    # The conversation must end with a human message, otherwise there is
    # nothing to reply to.
    if not messages or messages[-1]["role"] != "user":
        raise ValueError("Discussion Assistant needs a trailing human message to reply to.")

    # Message to the AI: the situation, which stage awaits a decision, the
    # chat history, and a request to reply conversationally.
    prompt = f"""
{_format_context(pipeline_state, [] if pipeline_state.get("from_scratch") or pipeline_state.get("force_from_scratch") else pipeline_state.get("applications", []))}

============================================================
CURRENTLY AWAITING A HUMAN DECISION ON: {pending_stage}
============================================================

============================================================
CONVERSATION SO FAR
============================================================

{_format_transcript(messages)}

============================================================
YOUR REPLY
============================================================

Reply to the human's latest message above, in plain conversational
text.
"""

    # Ask the AI for free-form text (not structured JSON).
    return llm.call_llm_text(prompt, system=load_skill("discuss"), model=model)
