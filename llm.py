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

Plain words: this is the one file that actually "talks to the AI". Every
agent hands it a question (a "prompt"); it sends that to Claude, waits,
and returns the answer. If the AI's reply is broken or missing, it simply
tries again (up to 3 times in total) before giving up.
"""

import asyncio
import contextlib
import json
from typing import Any, Callable, Dict, List, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    query,
)

import config
import token_log

# How many times we try a call before giving up.
MAX_ATTEMPTS = 3
# Generous even though these agents have no tools: a revision pass with
# a full architecture plan + review findings to digest is a lot to
# reason through, and max_turns=1 was observed to cut that reasoning
# short -- the model would return a structurally-valid but truncated
# result (e.g. an empty required array) rather than its actual answer.
# ("Turns" = back-and-forth steps the AI may take while thinking about one
# question; 6 gives it room to reason without running forever.)
MAX_TURNS = 6

# Token usage the CLI reported for the call(s) in progress (input, cached
# input, output); read and reset by call_llm_json for the token ledger.
_USAGE: Dict[str, int] = {}


# What the CLI reported about the call(s) in progress besides token counts: how many turns it took,
# how long, what it cost, and how much hidden reasoning it wrote. Read by call_llm_json for the ledger.
_STATS: Dict[str, float] = {}


def _note_stats(message: Any) -> None:
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ThinkingBlock):
                _STATS["thinking_tokens_est"] = _STATS.get("thinking_tokens_est", 0) + (len(block.thinking) + 3) // 4
    elif isinstance(message, ResultMessage):
        _STATS["turns"] = _STATS.get("turns", 0) + (message.num_turns or 0)
        _STATS["duration_s"] = round(_STATS.get("duration_s", 0) + (message.duration_ms or 0) / 1000, 1)
        _STATS["cost_usd"] = round(_STATS.get("cost_usd", 0) + (message.total_cost_usd or 0), 4)


def _options_kwargs() -> Dict[str, Any]:
    return {"max_turns": MAX_TURNS}


def _note_usage(message: Any) -> None:
    usage = getattr(message, "usage", None)
    if isinstance(usage, dict):
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
            if isinstance(usage.get(key), int):
                _USAGE[key] = _USAGE.get(key, 0) + usage[key]


# Errors that no retry can fix (out of quota, rate limited, not logged in). Retrying only burns
# calls, so the first one ends the attempts at once.
_UNRECOVERABLE = ("session limit", "usage limit", "hit your", "rate limit", "credit balance", "quota",
                  "not logged in", "please run /login", "authentication", "unauthorized", "invalid api key",
                  "permission denied", "billing")


class _Rejected(ValueError):
    """The answer arrived but failed our own content checks. Always worth another try, whatever
    words the message contains."""


def _unrecoverable(error: Exception) -> bool:
    if isinstance(error, _Rejected):
        return False
    text = str(error).lower()
    return any(marker in text for marker in _UNRECOVERABLE)


def _schema_for(required_keys: List[str]) -> Dict[str, Any]:
    # Build a simple "form" describing the answer we insist on: a JSON
    # object that MUST contain each of the named fields. The AI's reply
    # is checked against this form automatically.
    return {
        "type": "object",
        "properties": {key: {} for key in required_keys},
        "required": required_keys,
    }


async def _call_once(
    prompt: str, system: Optional[str], schema: Dict[str, Any], model: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    # Settings for this single AI call:
    #  system_prompt = the agent's standing instructions (its "job description")
    #  model         = which Claude model to use (default from config.py)
    #  tools=[]      = the AI may NOT use tools (no files, no web) - it only thinks
    #  output_format = force the reply to match the schema "form" above
    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model or config.MODEL_ID,
        tools=[],
        output_format={"type": "json_schema", "schema": schema},
        **_options_kwargs(),
    )

    # Let the async generator run to its own natural end rather than
    # returning the moment a ResultMessage is seen -- an early return
    # here triggers query()'s implicit aclose() concurrently with
    # asyncio.run()'s own event-loop teardown, which raises a spurious
    # "generator is already running" error on the next call.
    structured_output: Optional[Dict[str, Any]] = None
    # The AI streams back many messages while working; only the final
    # "result" message contains the finished, validated answer.
    async for message in query(prompt=prompt, options=options):
        _note_stats(message)
        if isinstance(message, ResultMessage):
            structured_output = message.structured_output
            _note_usage(message)

    return structured_output


def call_llm_json(
    prompt: str,
    agent_name: str,
    required_keys: Optional[List[str]] = None,
    system: Optional[str] = None,
    max_repair_attempts: int = 2,
    model: Optional[str] = None,
    validate: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """`model`, if given, overrides config.MODEL_ID for this call only
    -- every agents/*.py run() accepts its own `model` param so a run
    can choose a different Claude model per agent (see
    config.AGENT_NAMES, orchestrator/graph.py's `_model_for`).

    `validate`, if given, checks the answer's CONTENT (the schema only checks
    that the fields exist). If it raises, the answer is not accepted: the AI is
    asked again, this time told exactly what was wrong, so one bad answer (say
    an empty list where at least one item is required) does not end the whole
    run. It is retried like any other failure, and only after every attempt has
    failed does the error reach the caller."""
    # Refuse to send an empty question.
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("LLM prompt cannot be empty.")

    required_keys = list(required_keys or [])
    schema = _schema_for(required_keys)
    max_attempts = max(1, max_repair_attempts + 1)

    # Try the call; on any failure remember the error and try again, until
    # attempts run out.
    last_error: Optional[Exception] = None
    current_prompt = prompt
    _USAGE.clear()
    _STATS.clear()
    attempts_used = 0
    token_log.record("llm_start", agent_name, system_tokens=token_log.est_tokens(system or ""),
                     prompt_tokens=token_log.est_tokens(prompt), max_attempts=max_attempts)
    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        before = dict(_USAGE)
        try:
            # asyncio.run lets this ordinary (non-async) function use the
            # SDK's asynchronous call and wait for its answer.
            result = asyncio.run(_call_once(current_prompt, system, schema, model))
            if result is None:
                raise ValueError("Claude did not return structured output.")
            if validate is not None:
                try:
                    validate(result)
                except (ValueError, KeyError, TypeError, AttributeError) as problem:
                    # Not accepted. Next time, show the AI what was wrong with
                    # its answer (a KeyError/TypeError means a field was missing
                    # or the wrong shape).
                    detail = f"missing or malformed field {problem}" if not isinstance(problem, ValueError) else str(problem)
                    current_prompt = (
                        f"{prompt}\n\n============================================================\n"
                        f"YOUR PREVIOUS ANSWER WAS REJECTED\n"
                        f"============================================================\n\n"
                        f"{detail}\n\nCorrect exactly this and return the complete JSON again."
                    )
                    raise _Rejected(detail) from None
            token_log.record("llm_attempt", agent_name, attempt=attempt, ok=True,
                             usage={k: v - before.get(k, 0) for k, v in _USAGE.items()}, stats=dict(_STATS))
            token_log.record("llm_call", agent_name, ok=True, attempts=attempts_used,
                             answer_tokens_est=token_log.est_tokens(json.dumps(result, default=str)), stats=dict(_STATS),
                             system_tokens=token_log.est_tokens(system or ""),
                             prompt_tokens=token_log.est_tokens(prompt), usage=dict(_USAGE))
            return result
        except Exception as exc:  # noqa: BLE001 -- retried uniformly, re-raised below
            last_error = exc
            token_log.record("llm_attempt", agent_name, attempt=attempt, ok=False, error=str(exc)[:200],
                             usage={k: v - before.get(k, 0) for k, v in _USAGE.items()})
            if _unrecoverable(exc):
                break                      # no retry can help: stop now instead of paying for more
            if attempt == max_attempts:
                break

    # All attempts failed: report which agent failed and why.
    token_log.record("llm_call", agent_name, ok=False, attempts=attempts_used, stats=dict(_STATS),
                     system_tokens=token_log.est_tokens(system or ""),
                     prompt_tokens=token_log.est_tokens(prompt), usage=dict(_USAGE))
    raise ValueError(
        f"{agent_name} returned an invalid response after {attempts_used} attempt{'s' if attempts_used != 1 else ''}: {last_error}"
    )


async def _call_once_text(prompt: str, system: Optional[str], model: Optional[str] = None) -> str:
    # Same as _call_once, but for plain chat-style text answers (no
    # fixed structure required).
    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model or config.MODEL_ID,
        tools=[],
        **_options_kwargs(),
    )

    # Collect every piece of text the AI writes and glue them together.
    text_parts: List[str] = []
    async for message in query(prompt=prompt, options=options):
        _note_stats(message)
        if isinstance(message, ResultMessage):
            _note_usage(message)
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

    _USAGE.clear()
    _STATS.clear()
    text = asyncio.run(_call_once_text(prompt, system, model))
    token_log.record("llm_call", "Discussion", ok=bool(text), attempts=1,
                     system_tokens=token_log.est_tokens(system or ""), prompt_tokens=token_log.est_tokens(prompt),
                     usage=dict(_USAGE), stats=dict(_STATS))
    if not text:
        raise ValueError("Claude did not return a text response.")
    return text
