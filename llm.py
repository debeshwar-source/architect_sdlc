"""
Claude Agent SDK client.

Unlike the granular claude_native_sdlc engine, these agents reason
over a small, pre-loaded knowledge base rather than exploring a live
repository, so no tools are needed here at all -- every call is a
single structured request. call_llm_json() keeps the exact signature
every agent already calls (prompt, agent_name, required_keys, system)
so the agents themselves are unchanged from the Bedrock build; only
this file talks to a different backend.

Structured output is schema-validated by the CLI itself via
output_format (see README), which replaces the Bedrock build's
regex-based JSON extraction + text repair-prompt loop -- a synthetic
"object with these required keys" schema is built from required_keys
since callers here only ever specify key names, not full per-field
types.

Auth: the SDK drives the bundled Claude Code CLI, which authenticates
exactly like an interactive Claude Code session (a Claude subscription
login, or ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN if set) -- no separate
API key required. See README.md.
"""

import asyncio
from typing import Any, Dict, List, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

import config

MAX_ATTEMPTS = 3
# Generous even though these agents have no tools: a revision pass with
# a full architecture plan + review findings to digest is a lot to
# reason through, and max_turns=1 was observed to cut that reasoning
# short -- the model would return a structurally-valid but truncated
# result (e.g. an empty required array) rather than its actual answer.
MAX_TURNS = 6


def _schema_for(required_keys: List[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {key: {} for key in required_keys},
        "required": required_keys,
    }


async def _call_once(
    prompt: str, system: Optional[str], schema: Dict[str, Any], model: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model or config.MODEL_ID,
        tools=[],
        output_format={"type": "json_schema", "schema": schema},
        max_turns=MAX_TURNS,
    )

    # Let the async generator run to its own natural end rather than
    # returning the moment a ResultMessage is seen -- an early return
    # here triggers query()'s implicit aclose() concurrently with
    # asyncio.run()'s own event-loop teardown, which raises a spurious
    # "generator is already running" error on the next call.
    structured_output: Optional[Dict[str, Any]] = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            structured_output = message.structured_output

    return structured_output


def call_llm_json(
    prompt: str,
    agent_name: str,
    required_keys: Optional[List[str]] = None,
    system: Optional[str] = None,
    max_repair_attempts: int = 2,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """`model`, if given, overrides config.MODEL_ID for this call only
    -- every agents/*.py run() accepts its own `model` param so a run
    can choose a different Claude model per agent (see
    config.AGENT_NAMES, orchestrator/graph.py's `_model_for`)."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("LLM prompt cannot be empty.")

    required_keys = list(required_keys or [])
    schema = _schema_for(required_keys)
    max_attempts = max(1, max_repair_attempts + 1)

    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = asyncio.run(_call_once(prompt, system, schema, model))
            if result is None:
                raise ValueError("Claude did not return structured output.")
            return result
        except Exception as exc:  # noqa: BLE001 -- retried uniformly, re-raised below
            last_error = exc
            if attempt == max_attempts:
                break

    raise ValueError(
        f"{agent_name} returned an invalid response after {max_attempts} attempts: {last_error}"
    )


async def _call_once_text(prompt: str, system: Optional[str], model: Optional[str] = None) -> str:
    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model or config.MODEL_ID,
        tools=[],
        max_turns=MAX_TURNS,
    )

    text_parts: List[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)

    return "".join(text_parts).strip()


def call_llm_text(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Free-form text completion -- the conversational counterpart to
    call_llm_json(). Used only by the Discussion Assistant
    (agents/discuss.py): a chat reply has no fixed shape to validate,
    so this skips output_format/schema entirely and returns whatever
    text Claude said, instead of structured JSON."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("LLM prompt cannot be empty.")

    text = asyncio.run(_call_once_text(prompt, system, model))
    if not text:
        raise ValueError("Claude did not return a text response.")
    return text
