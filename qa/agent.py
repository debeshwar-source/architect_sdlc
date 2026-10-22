"""The QA agent: works out which test cases can be checked against merged code, then checks the ones a person chose.

Plain words: it has the merged repository in front of it (Read, Search, and a shell to run the project's own tests) and works like a QA
engineer who can read code but has no running app, database, device or network. It never changes the repository. Two jobs:
  assess()       for every test case: can it be checked from here, how, and what stops it?  (nothing is run)
  verify_case()  carry out ONE test case the person chose and report pass / fail / inconclusive, with proof.
"""

import json
from typing import Any, Callable, Dict, List, Optional

from agents._common import load_skill
from coding import agent as coding_agent
from review import reviewer

ASSESS_TURNS = 80
VERIFY_TURNS = 80
DIFF_CHARS = 60_000
VERDICTS = ("can_verify", "partial", "cannot_verify", "out_of_scope")
METHODS = ("code_inspection", "unit_tests", "both", "none")
RESULTS = ("pass", "fail", "inconclusive")
SEVERITIES = ("blocker", "major", "minor")

ASSESS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "verdict": {"enum": list(VERDICTS)}, "method": {"enum": list(METHODS)}, "plan": {"type": "string"},
            "blockers": {"type": "string"}, "commands": {"type": "array", "items": {"type": "string"}}}}},
    },
    "required": ["summary", "items"],
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"enum": list(RESULTS)}, "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"enum": list(SEVERITIES)}, "title": {"type": "string"}, "detail": {"type": "string"}, "where": {"type": "string"}}}},
        "commands_run": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["result", "summary", "evidence", "issues"],
}


def _compact(case: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in case.items() if v not in ("", [], None)}


def _context(pr: Dict[str, Any], files: List[Dict[str, Any]]) -> str:
    changed = "\n".join(f"- {f['path']} ({f['status']}, +{f['added']} -{f['removed']})" for f in files[:200]) or "(unknown)"
    return (f"# Merged pull request #{pr['number']}: {pr['title']}\nBranch {pr['branch']} merged into {pr['base']} "
            f"(commit {pr.get('merge_sha', '')[:10]}).\n\n## Description\n{pr.get('body') or '(none)'}\n\n## Files changed\n{changed}\n")


def assess(pr: Dict[str, Any], plan: Dict[str, Any], files: List[Dict[str, Any]], diff_text: str, tree: str, model: Optional[str] = None,
           on_event: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """The QA agent's assessment of every test case. Returns {'summary', 'items': {id: {verdict, method, plan, blockers, commands, checkable}}}.
    A test case the agent skipped is reported as not verifiable (never silently dropped, never assumed checkable)."""
    cut = diff_text[:DIFF_CHARS] + ("\n... (diff cut; read the rest with git in the shell)" if len(diff_text) > DIFF_CHARS else "")
    ac = "\n".join(f"- {a['id']}: {a['text']}" for a in plan["acceptance_criteria"]) or "(none)"
    prompt = (_context(pr, files) + f"\n## Diff of the pull request\n{cut}\n\n## Acceptance criteria\n{ac}\n\n"
              f"## Test cases ({len(plan['test_cases'])})\n" + json.dumps([_compact(c) for c in plan["test_cases"]], indent=1))
    if on_event:
        on_event("The QA agent is reading the merged code and the test cases…")
    # read and search only: nothing is run until a person has chosen what may be checked (the shell comes with the checks)
    answer = coding_agent._call("qa-assess", "qa", prompt, load_skill("qa_assess"), tree, model, None, on_event, ASSESS_SCHEMA,
                                reviewer.READ_ONLY_TOOLS, reviewer.DISALLOWED, ASSESS_TURNS)["answer"]
    return clean_assessment(answer, plan["test_cases"])


def clean_assessment(answer: Dict[str, Any], cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    got = {str(i.get("id")): i for i in answer.get("items") or [] if isinstance(i, dict)}
    items: Dict[str, Dict[str, Any]] = {}
    for c in cases:
        raw = got.get(c["id"])
        if not raw or raw.get("verdict") not in VERDICTS:
            items[c["id"]] = {"verdict": "cannot_verify", "method": "none", "plan": "", "commands": [], "checkable": False,
                              "blockers": "The QA agent gave no usable assessment for this test case."}
            continue
        verdict = raw["verdict"]
        items[c["id"]] = {"verdict": verdict, "method": raw.get("method") if raw.get("method") in METHODS else "none",
                          "plan": " ".join(str(raw.get("plan") or "").split()), "blockers": " ".join(str(raw.get("blockers") or "").split()),
                          "commands": [str(x) for x in raw.get("commands") or [] if str(x).strip()][:5], "checkable": verdict in ("can_verify", "partial")}
    return {"summary": str(answer.get("summary") or "").strip(), "items": items}


def verify_case(pr: Dict[str, Any], case: Dict[str, Any], plan: Dict[str, Any], assessed: Dict[str, Any], tree: str, model: Optional[str] = None,
                guidance: str = "", on_event: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Check ONE test case against the merged code. Returns a cleaned result: {'result','summary','evidence','issues','commands_run'}."""
    refs = [a for a in plan["acceptance_criteria"] if a["id"] in case["requirement_refs"]]
    prompt = (f"# Merged pull request #{pr['number']}: {pr['title']} (commit {pr.get('merge_sha', '')[:10]})\n\n{pr.get('body') or ''}\n\n"
              "## The test case to check\n" + json.dumps(_compact(case), indent=1) + "\n\n## Acceptance criteria it refers to\n"
              + ("\n".join(f"- {a['id']}: {a['text']}" for a in refs) or "(none)")
              + (f"\n\n## What the earlier assessment planned\n{assessed.get('plan')}" + (f"\nKnown limits: {assessed['blockers']}" if assessed.get("blockers") else "")
                 if assessed else "")
              + (f"\n\n## The person's guidance for this check\n{guidance.strip()}" if guidance.strip() else ""))
    answer = coding_agent._call("qa-verify", "qa", prompt, load_skill("qa_verify"), tree, model, None, on_event, VERIFY_SCHEMA,
                                reviewer.TOOLS, reviewer.DISALLOWED, VERIFY_TURNS)["answer"]
    return clean_result(answer)


def clean_result(answer: Dict[str, Any]) -> Dict[str, Any]:
    """A result the person can rely on: a pass with no proof is not a pass, and a failure always names at least one issue."""
    result = answer.get("result") if answer.get("result") in RESULTS else "inconclusive"
    evidence = [" ".join(str(e).split()) for e in answer.get("evidence") or [] if str(e).strip()]
    issues = [{"severity": i.get("severity") if i.get("severity") in SEVERITIES else "major", "title": " ".join(str(i.get("title") or "").split()),
               "detail": " ".join(str(i.get("detail") or "").split()), "where": str(i.get("where") or "").strip()}
              for i in answer.get("issues") or [] if isinstance(i, dict) and (str(i.get("title") or "").strip() or str(i.get("detail") or "").strip())]
    summary = " ".join(str(answer.get("summary") or "").split())
    if result == "pass" and not evidence:
        result, summary = "inconclusive", (summary + " " if summary else "") + "(No proof was given for this pass, so it is not accepted as one.)"
    if result == "fail" and not issues:
        issues = [{"severity": "major", "title": summary[:120] or "The test case failed", "detail": summary, "where": ""}]
    return {"result": result, "summary": summary, "evidence": evidence, "issues": issues,
            "commands_run": [str(c) for c in answer.get("commands_run") or [] if str(c).strip()][:20]}
