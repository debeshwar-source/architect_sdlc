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
"""

import json
from typing import Any, Dict, List, Optional

import llm
from agents._common import load_skill
from knowledge_base.loader import ApplicationDoc, format_full_kb_for_prompt

# (pipeline_state key, section label) -- in pipeline order. Unlike
# agents/_common.py's format_dict(), this deliberately does NOT strip
# clarifying_questions: those are exactly the kind of thing a human
# would want to discuss, and nothing here ever feeds another agent.
STAGE_RESULT_LABELS = [
    ("requirement", "REQUIREMENT"),
    ("architecture", "ARCHITECTURE PLAN"),
    ("quality", "TEST STRATEGY"),
    ("implementation_spec", "IMPLEMENTATION SPECS"),
    ("review", "REVIEW OUTCOME"),
    ("release_plan", "RELEASE PLAN"),
]


def _format_context(pipeline_state: Dict[str, Any], applications: List[ApplicationDoc]) -> str:
    sections = [
        "============================================================",
        "KNOWLEDGE BASE",
        "============================================================",
        "",
        format_full_kb_for_prompt(applications),
    ]
    for key, label in STAGE_RESULT_LABELS:
        value = pipeline_state.get(key)
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
    return "\n".join(
        f"{'HUMAN' if m['role'] == 'user' else 'YOU'}: {m['content']}" for m in messages
    )


def reply(
    messages: List[Dict[str, str]],
    pipeline_state: Dict[str, Any],
    pending_stage: str,
    model: Optional[str] = None,
) -> str:
    if not messages or messages[-1]["role"] != "user":
        raise ValueError("Discussion Assistant needs a trailing human message to reply to.")

    prompt = f"""
{_format_context(pipeline_state, pipeline_state.get("applications", []))}

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

    return llm.call_llm_text(prompt, system=load_skill("discuss"), model=model)
