"""Circuit breaker for the automated Review -> Architecture retry loop.

MAX_REVIEW_ITERATIONS only bounds *how many* retries happen; it can't
tell a retry that is converging from one that is spinning. This reads
the retry history and escalates in three steps:

  ok         no BLOCKING finding has recurred -- plain retry
  steer      a BLOCKING finding recurred -> retry, but tell the agent
             its last fix didn't land and to take a different approach
  constrain  it recurred AND the plan grew (more affected applications),
             or it survived a steer -> retry with scope frozen: no new applications, no
             widening; fix the findings inside the current footprint
  stop       it recurred again after a constrain -> stop and escalate
             to a human instead of burning another pass

Pure functions over plain dicts, no I/O, so the ladder is unit-testable.
"""

import re
from typing import Dict, List

OK, STEER, CONSTRAIN, STOP = "ok", "steer", "constrain", "stop"

_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "not", "are", "was", "has",
    "have", "from", "into", "should", "must", "will", "does", "but", "its",
}
# Two findings count as "the same finding" at or above this token overlap.
SIMILARITY_THRESHOLD = 0.5

DIRECTIVES = {
    STEER: (
        "CIRCUIT BREAKER (steer): at least one BLOCKING finding below was "
        "already raised on the previous pass and your last revision did not "
        "resolve it. Do not re-apply the same fix. Take a different approach "
        "to those specific findings, or state plainly why the finding is wrong."
    ),
    CONSTRAIN: (
        "CIRCUIT BREAKER (constrain): findings are recurring AND the plan has "
        "been growing. Scope is now frozen: do not add new applications or "
        "widen the affected set. Resolve the findings inside the current "
        "footprint, shrinking scope if that is what it takes."
    ),
}


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2 and t not in _STOPWORDS}


def _similar(a: set, b: set) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= SIMILARITY_THRESHOLD


def blocking_signatures(findings: List[dict]) -> List[List[str]]:
    return [
        sorted(_tokens(f.get("finding", "")))
        for f in findings or []
        if f.get("severity") == "BLOCKING"
    ]


def _recurring(prev: List[List[str]], curr: List[List[str]]) -> int:
    return sum(1 for c in curr if any(_similar(set(c), set(p)) for p in prev))


def evaluate(breaker: Dict, iteration: int, findings: List[dict], affected_count: int) -> Dict:
    """Returns the new breaker state. `breaker` is the prior state (may
    be empty); `iteration` is the architecture pass the review just
    judged. Re-reviewing the same iteration (a human re-run) replaces
    that iteration's record rather than appending a second one."""
    records = [r for r in (breaker or {}).get("records", []) if r["iteration"] != iteration]
    prior_level = (breaker or {}).get("level", OK)
    current = {
        "iteration": iteration,
        "signatures": blocking_signatures(findings),
        "affected_count": affected_count,
    }
    previous = records[-1] if records else None
    records.append(current)

    if not current["signatures"] or previous is None:
        return {"level": OK, "reason": "", "recurring": 0, "records": records}

    recurring = _recurring(previous["signatures"], current["signatures"])
    if recurring == 0:
        return {"level": OK, "reason": "", "recurring": 0, "records": records}

    grew = current["affected_count"] > previous["affected_count"]
    if prior_level == CONSTRAIN:
        level, reason = STOP, f"{recurring} finding(s) still recurring after scope was frozen"
    elif grew:
        level, reason = CONSTRAIN, (
            f"{recurring} finding(s) recurring while affected applications grew "
            f"{previous['affected_count']} -> {current['affected_count']}"
        )
    elif prior_level == STEER:
        level, reason = CONSTRAIN, f"{recurring} finding(s) still recurring after a steer"
    else:
        level, reason = STEER, f"{recurring} BLOCKING finding(s) recurred from the previous pass"
    return {"level": level, "reason": reason, "recurring": recurring, "records": records}


def directive_for(breaker: Dict) -> str:
    return DIRECTIVES.get((breaker or {}).get("level"), "")
