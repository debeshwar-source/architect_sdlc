"""Shared helpers for architecture-level agents."""

import json
import re
from functools import lru_cache
from typing import Any, Dict, List

import config


@lru_cache(maxsize=None)
def load_skill(name: str) -> str:
    """Read a skill file once per process; skills are static within a
    run. name is the skill's filename without extension, e.g.
    'architecture' for skills/architecture.md."""
    path = config.SKILLS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Skill file not found: {path}")
    return path.read_text(encoding="utf-8")


def format_dict(data: Dict[str, Any]) -> str:
    """Serializes a PRIOR stage's already-finished output for inclusion
    in a downstream agent's prompt -- every call site in this package
    uses it that way, never to serialize a stage's own just-produced
    output. `clarifying_questions` is dropped here for exactly that
    reason: it's a human-facing negotiation artifact -- an ambiguity
    the producing stage flagged, together with the options it was
    weighing -- for whoever reviews THAT stage, not reasoning material
    for the NEXT agent. By the time a stage's output is handed
    forward, its ambiguity must already be resolved and reflected in
    its actual output fields (a chosen `role_in_change`, a chosen
    scope, etc.); forwarding the raw question+options alongside that
    would hand the next agent unresolved ambiguity to guess at, or a
    stale menu of options a human already decided on."""
    if not data:
        return json.dumps(data or {}, indent=2, default=str)
    cleaned = {k: v for k, v in data.items() if k != "clarifying_questions"}
    return json.dumps(cleaned, indent=2, default=str)


def format_feedback_section(
    revision_feedback: Dict[str, Any] = None, context_notes: str = ""
) -> str:
    """`context_notes` is pre-formatted background from the hive
    (recalled past runs, standing human decisions -- see hive/); it is
    appended after any revision feedback and is empty for a run with
    nothing to recall, so behavior is unchanged in that case."""
    if not revision_feedback:
        return context_notes
    return f"""
============================================================
PRIOR REVIEW FEEDBACK (revision pass -- address this)
============================================================

The previous version of this plan was reviewed and found to have
issues. Address them; do not just resubmit the same output. If this
part of the plan was actually correct and the issue lies elsewhere,
say so plainly rather than changing something that wasn't wrong.

If any of this feedback is a human's answer to one of your own prior
`clarifying_questions`, that ambiguity is now RESOLVED: fold their
answer into your actual output and do not surface that same question
again -- a resolved ambiguity is not a genuine one anymore.

{format_dict(revision_feedback)}
{context_notes}"""


MAX_CLARIFYING_QUESTIONS = 2

# Enforced by validate_clarifying_questions (and stated in every skill's
# rules): a question is read on a card, so it must be answerable without
# reading the investigation behind it.
MAX_QUESTION_CHARS = 280
MAX_OPTION_CHARS = 120


def _shorten(text: str, limit: int) -> str:
    """Keeps whole sentences up to `limit` chars; a single over-long
    sentence is hard-cut with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    kept = ""
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    return kept or text[: limit - 1].rstrip() + "…"


def validate_clarifying_questions(result: Dict[str, Any], agent_name: str) -> None:
    """Shared shape check for the `clarifying_questions` field every
    agent in this package now returns (see agents/_common.py's
    CLARIFYING_QUESTIONS_RULE, embedded in every skill file). Never
    raises: this is an optional, best-effort UI convenience -- the
    agent's actual, load-bearing output is never contingent on it, so
    a malformed entry is repaired or dropped here rather than failing
    an entire run over what is, at worst, an LLM phrasing quirk in a
    secondary feature (observed in production: a model paraphrasing
    its own recommendation instead of repeating an option's exact
    text, which used to hard-crash the run right here). Mutates
    result["clarifying_questions"] in place."""
    questions = result.get("clarifying_questions")
    if not isinstance(questions, list):
        result["clarifying_questions"] = []
        return

    cleaned: List[Dict[str, Any]] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        question_text = q.get("question")
        options = q.get("options")
        if not question_text or not isinstance(options, list) or len(options) < 2:
            continue

        recommended = q.get("recommended_option")
        if recommended not in options:
            recommended = None

        # Shorten AFTER matching the recommendation so trimming an
        # option can't break it; re-point it at its trimmed twin.
        shortened = [_shorten(str(o), MAX_OPTION_CHARS) for o in options]
        if recommended is not None:
            recommended = shortened[options.index(recommended)]

        cleaned.append(
            {
                "question": _shorten(str(question_text), MAX_QUESTION_CHARS),
                "options": shortened,
                "recommended_option": recommended,
            }
        )

    # Cap applied to the CLEANED list, not the raw one -- a malformed
    # entry occupying an early slot must never crowd out a later, valid
    # one just because of raw positional bad luck.
    result["clarifying_questions"] = cleaned[:MAX_CLARIFYING_QUESTIONS]


# Embedded verbatim in every skills/*.md file's own "## Rules" section,
# right after the domain-specific rules -- kept here as the single
# source of truth for what the behavior should be, even though the
# skill files themselves are plain static markdown and this constant
# is not (and cannot be) programmatically injected into them. When
# editing this, edit the matching prose in every skill file to match.
CLARIFYING_QUESTIONS_RULE = """
- This system's plans are reviewed by CXO-level stakeholders, not
  engineers -- `clarifying_questions` is for genuine, material
  ambiguity ONLY: one where a wrong guess would change a real business
  outcome (cost, timeline, risk, customer impact), not an
  implementation or engineering detail a delivery team would settle
  on its own without ever escalating it. At most 2 -- do not
  manufacture questions to seem thorough; an empty list is the
  normal, expected outcome when nothing clears that bar. Each entry
  needs 2-4 concrete, mutually exclusive options framed in terms of
  this stage's own output (never a generic "yes/no"), plus which one
  you'd recommend in `recommended_option`. This is not a substitute
  for making a defensible call yourself: produce your actual output
  using your own best judgment regardless of whether a human ever
  answers -- the question just captures the alternative you didn't
  take, for a human to resolve. Once it IS resolved -- a human
  answered it, surfaced as feedback on a later revision pass -- it is
  no longer genuine; do not ask it again.
  Phrase each question so it stands alone: ONE sentence (at most 280
  characters) stating exactly what decision you need, then short
  options (at most 120 characters each). No findings, background or
  reasoning inside the question -- that belongs in your normal output.
  If it needs a paragraph to explain, it is a report, not a question.
"""

