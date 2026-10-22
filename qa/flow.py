"""QA after a merge: a person chooses what the QA agent may check -> it checks it -> a person decides, test case by test case.

Plain words: one saved record per merged pull request (runs/qa/). The QA agent only ever works on what a person chose. Every decision is
per test case and always a person's: a passed test case is signed off by name; a failed or doubtful one is either overridden (by name, with
a written reason) or sent to the coding agent. There is no sign-off of the whole record. A new check of a test case takes back its old
decision. The record is saved after every step, and after every single check, so closing the app loses nothing. The rules below are
enforced here, not just on the screen.
"""

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import config
from coding import agent as coding_agent
from qa import agent, plan as qa_plan
from review import checkout, github

STORE = config.RUNS_ROOT / "qa"
OnEvent = Optional[Callable[[str], None]]


class QAError(Exception):
    """Something the person must fix or decide; the message is fit to show."""


# ---------- saved record ----------

def _path(owner: str, name: str, number: int) -> Path:
    return STORE / f"{owner}__{name}__{number}.json".lower()


def _migrate(state: Dict[str, Any]) -> Dict[str, Any]:
    """Records saved before decisions were per test case: an accepted risk becomes an override of that test case. An old whole-record decision
    is dropped (it is kept in the history), because there is no such thing any more."""
    state.setdefault("signoffs", {})
    for tc, w in (state.pop("waivers", None) or {}).items():
        state["signoffs"].setdefault(tc, {"decision": "overridden", "by": w.get("by", ""), "at": w.get("at", ""), "note": w.get("reason", ""),
                                          "qa_result": w.get("was", "fail")})
    old = state.pop("signoff", None)
    if old:
        state.setdefault("history", []).append(f"An earlier whole-record decision ({old.get('decision')} by {old.get('by')}) was dropped: decisions are per test case now")
    return state


def load(owner: str, name: str, number: int) -> Optional[Dict[str, Any]]:
    try:
        return _migrate(json.loads(_path(owner, name, number).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def save(state: Dict[str, Any]) -> None:
    path = _path(*state["repo"], state["number"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def discard(state: Dict[str, Any]) -> None:
    _path(*state["repo"], state["number"]).unlink(missing_ok=True)


def start(owner: str, name: str, pr: Dict[str, Any], test_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Begin QA for a merged pull request against a test plan."""
    state = {"repo": [owner, name], "number": pr["number"],
             "pr": {k: pr.get(k, "") for k in ("title", "url", "branch", "base", "merge_sha", "merged_at", "body", "number")},
             "run_id": test_plan["run_id"], "plan_title": test_plan["title"], "plan_summary": test_plan["summary"],
             "cases": test_plan["test_cases"], "acceptance_criteria": test_plan["acceptance_criteria"],
             "assessment": None, "selected": [], "results": {}, "signoffs": {}, "history": [], "error": None,
             "fixes": {}, "code_sha": pr.get("merge_sha", "")}
    save(state)
    return state


def phase(state: Dict[str, Any]) -> str:
    return "review" if state["results"] else "select"


def case_of(state: Dict[str, Any], tc_id: str) -> Dict[str, Any]:
    return next(c for c in state["cases"] if c["id"] == tc_id)


def _order(state: Dict[str, Any], ids: List[str]) -> List[str]:
    """Most important first, then in the plan's own order."""
    rank = {c["id"]: (qa_plan.PRIORITY.get(c["priority"], 9), i) for i, c in enumerate(state["cases"])}
    return sorted(ids, key=lambda i: rank.get(i, (9, 0)))


def _say(state: Dict[str, Any], text: str) -> None:
    state["history"].append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}")


# ---------- 1. assess ----------

def run_assess(state: Dict[str, Any], model: Optional[str] = None, on_event: OnEvent = None) -> Dict[str, Any]:
    """The QA agent reads the merged code and says, for each test case, whether it can be checked here. Nothing is run or changed."""
    sha = state["pr"].get("merge_sha")
    if not sha:
        raise QAError("GitHub gave no merge commit for this pull request, so the merged code cannot be opened.")
    owner, name = state["repo"]
    try:
        tree = str(checkout.prepare_commit(owner, name, sha))
        files = github.pr_files(owner, name, state["number"])
        diff = github.get_diff(owner, name, state["number"])
        plan = {"test_cases": state["cases"], "acceptance_criteria": state["acceptance_criteria"]}
        out = agent.assess(state["pr"], plan, files, diff, tree, model, on_event)
    except (checkout.CheckoutError, github.GithubError) as exc:
        state["error"] = str(exc)
        save(state)
        raise QAError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 -- shown to the person, who can try again
        state["error"] = f"{type(exc).__name__}: {exc}"
        save(state)
        raise QAError(state["error"]) from None
    state["error"] = None
    state["assessment"] = {**out, "tree": tree}
    state["selected"] = _order(state, [i for i, a in out["items"].items() if a["checkable"]])      # a suggestion: the person decides
    state["tick_epoch"] = state.get("tick_epoch", 0) + 1
    _say(state, f"The QA agent assessed {len(state['cases'])} test cases: "
                f"{sum(a['checkable'] for a in out['items'].values())} can be checked here.")
    save(state)
    return state


def set_selection(state: Dict[str, Any], ids: List[str], redraw: bool = False) -> None:
    """Remember which test cases are chosen. `redraw` makes the screen's ticks follow it (select all / clear)."""
    known = {c["id"] for c in state["cases"]}
    state["selected"] = _order(state, [i for i in ids if i in known])
    if redraw:
        state["tick_epoch"] = state.get("tick_epoch", 0) + 1
    save(state)


# ---------- 2. check what the person chose ----------

def run_checks(state: Dict[str, Any], model: Optional[str] = None, on_event: OnEvent = None, only: Optional[List[str]] = None,
               guidance: str = "", skip_review: bool = False) -> Dict[str, Any]:
    """Run QA on the test cases the person chose, one at a time, saving after each. With `only`, exactly those (a first run or a redo: any test
    case in the plan); without it, the chosen ones not yet checked. The agent never gets a test case the person did not choose."""
    known = {c["id"] for c in state["cases"]}
    chosen = [i for i in (state["selected"] if only is None else only) if i in known]
    todo = _order(state, [i for i in chosen if only is not None or i not in state["results"]])
    if not todo:
        raise QAError("Nothing is chosen." if not chosen else "Everything chosen has already been checked.")
    state["selected"] = _order(state, list(set(state["selected"]) | set(todo)))
    from qa import fix as qa_fix
    for tc_id in todo:                                  # a test case with a fix pending, or not yet cleared by the reviewer, is not tested yet
        why = qa_fix.retest_blocker(state, tc_id, skip_review)
        if why:
            raise QAError(f"{tc_id}: {why}")
    plan = {"acceptance_criteria": state["acceptance_criteria"]}
    state["error"] = None
    for n, tc_id in enumerate(todo, start=1):
        case = case_of(state, tc_id)
        if on_event:
            on_event(f"Checking {tc_id} ({n} of {len(todo)}): {case['title'][:70]}")
        try:
            tree, tested = qa_fix.tree_for(state, tc_id)         # the merged code, or this case's fix
        except checkout.CheckoutError as exc:
            raise QAError(str(exc)) from None
        try:
            result = agent.verify_case(state["pr"], case, plan, ((state.get("assessment") or {}).get("items") or {}).get(tc_id) or {}, tree, model, guidance, on_event)
        except coding_agent.Incomplete as exc:
            if "ran out of turns" not in str(exc):
                _stopped(state, tc_id, exc)
            # a check too big for the agent's budget is a real finding about that test case: it could not be settled
            result = {"result": "inconclusive", "summary": "The QA agent ran out of room before it could settle this test case; check it by hand "
                      "or narrow it down and check again.", "evidence": [], "issues": [], "commands_run": []}
        except Exception as exc:  # noqa: BLE001 -- the agent was stopped or crashed: that is not a test result
            _stopped(state, tc_id, exc)
        prior = state["results"].get(tc_id) or {}
        state["results"][tc_id] = {**result, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "attempt": prior.get("attempt", 0) + 1, "tested": tested}
        fx = qa_fix.latest(state, tc_id)
        if fx and result["result"] == "pass":
            fx["verified"] = True                               # the fix did what it was for
        if state["signoffs"].pop(tc_id, None):          # a new result needs a new decision
            _say(state, f"{tc_id}: its earlier decision was taken back by the new check")
        _say(state, f"{tc_id}: {result['result']}")
        save(state)
    return state


def _stopped(state: Dict[str, Any], tc_id: str, exc: Exception) -> None:
    """The agent stopped or broke in the middle of a check. Nothing is recorded for that test case (a stopped agent has found nothing, and
    must not count against the code), the checks already done stay, the run ends, and the person can start it again."""
    state["error"] = f"The QA agent stopped while checking {tc_id}, so nothing was recorded for it or for the ones after it. Run the checks again. ({type(exc).__name__}: {str(exc)[:200]})"
    _say(state, f"The run stopped at {tc_id} (no result recorded)")
    save(state)
    raise QAError(state["error"]) from None


# ---------- 3. what is decided, test case by test case ----------

def counts(state: Dict[str, Any]) -> Dict[str, int]:
    """Where the test cases stand. `waiting` are checked test cases nobody has decided yet (passed and unsigned, failed, or in doubt)."""
    res, sig = state["results"], state["signoffs"]
    return {"checked": len(res), "passed": sum(r["result"] == "pass" for r in res.values()), "failed": sum(r["result"] == "fail" for r in res.values()),
            "inconclusive": sum(r["result"] == "inconclusive" for r in res.values()),
            "signed_off": sum(d["decision"] == "signed_off" for d in sig.values()), "overridden": sum(d["decision"] == "overridden" for d in sig.values()),
            "waiting": sum(i not in sig for i in res), "not_checked": len(state["cases"]) - len(res)}


def decision_of(state: Dict[str, Any], tc_id: str) -> Optional[Dict[str, Any]]:
    return state["signoffs"].get(tc_id)


def not_covered(state: Dict[str, Any]) -> List[str]:
    """The test cases nobody checked here: not chosen, or the QA agent could not check them. Nothing here covers these."""
    return _order(state, [c["id"] for c in state["cases"] if c["id"] not in state["results"]])


def sign_off_case(state: Dict[str, Any], tc_id: str, by: str) -> Dict[str, Any]:
    """The person signs off ONE test case that QA passed, by name. Always a person's act: a pass alone decides nothing."""
    r = state["results"].get(tc_id)
    if not r:
        raise QAError(f"{tc_id} has not been checked yet.")
    if r["result"] != "pass":
        raise QAError(f"{tc_id} did not pass, so it cannot be signed off. Override it with a reason, or have it fixed.")
    if not by.strip():
        raise QAError("Sign with your name: the record says who signed off.")
    state["signoffs"][tc_id] = {"decision": "signed_off", "by": by.strip(), "at": time.strftime("%Y-%m-%d %H:%M:%S"), "note": "",
                                "qa_result": "pass", "attempt": r.get("attempt", 1)}
    _say(state, f"{tc_id}: signed off by {by.strip()}")
    save(state)
    return state


def override_case(state: Dict[str, Any], tc_id: str, by: str, reason: str) -> Dict[str, Any]:
    """The person overrides QA on ONE failed or doubtful test case, by name and with a written reason. Only that test case is affected."""
    r = state["results"].get(tc_id)
    if not r or r["result"] not in ("fail", "inconclusive"):
        raise QAError("Only a failed or inconclusive test case can be overridden.")
    if not by.strip():
        raise QAError("Sign with your name: the record says who overrode QA.")
    if not reason.strip():
        raise QAError("Say why: the reason goes into the record.")
    state["signoffs"][tc_id] = {"decision": "overridden", "by": by.strip(), "at": time.strftime("%Y-%m-%d %H:%M:%S"), "note": reason.strip(),
                                "qa_result": r["result"], "attempt": r.get("attempt", 1)}
    _say(state, f"{tc_id}: QA overridden by {by.strip()}")
    save(state)
    return state


def take_back(state: Dict[str, Any], tc_id: str) -> Dict[str, Any]:
    """Take back one test case's decision (it stays in the history)."""
    d = state["signoffs"].pop(tc_id, None)
    if not d:
        raise QAError("Nothing was decided for that test case.")
    _say(state, f"{tc_id}: the decision by {d['by']} was taken back")
    save(state)
    return state


# ---------- the record, written out ----------

BADGE = {"pass": "✅ Pass", "fail": "❌ Fail", "inconclusive": "❔ Inconclusive"}


def report(state: Dict[str, Any]) -> str:
    """The QA record as Markdown: for every checked test case what QA found and what the person decided, and what was not checked."""
    pr, c = state["pr"], counts(state)
    out = [f"# QA record: pull request #{state['number']} · {pr['title']}", "",
           f"- Test plan: {state['plan_title']} (run `{state['run_id']}`), {len(state['cases'])} test cases",
           f"- Merged: {pr.get('merged_at', '')} · commit `{pr.get('merge_sha', '')[:10]}`",
           f"- {c['signed_off']} signed off · {c['overridden']} overridden · {c['waiting']} waiting for a decision · {c['not_checked']} not checked",
           "", "## Test cases", "", "| Test case | Priority | QA found | Decision |", "|---|---|---|---|"]
    for i in _order(state, list(state["results"])):
        r, case, d = state["results"][i], state["cases"][[x["id"] for x in state["cases"]].index(i)], state["signoffs"].get(i)
        decision = (f"Signed off by {d['by']}" if d["decision"] == "signed_off" else f"Overridden by {d['by']}: {d['note']}") if d else "Waiting"
        out.append(f"| {i} {case['title']} | {case['priority']} | {BADGE[r['result']]} {r['summary'].replace('|', '/')} | {decision.replace('|', '/')} |")
    for i in _order(state, list(state["results"])):
        r = state["results"][i]
        if r["issues"]:
            out += ["", f"### {i}"] + [f"- **{x['severity']}** {x['title']}: {x['detail']}" + (f" (`{x['where']}`)" if x["where"] else "") for x in r["issues"]]
    missing = not_covered(state)
    if missing:
        out += ["", "## Not checked here", "", ", ".join(missing)]
    return "\n".join(out) + "\n"


def post_report(state: Dict[str, Any]) -> str:
    """Put the record on the pull request as a comment (the person asked for it). Returns the comment's address."""
    if not state["signoffs"]:
        raise QAError("Decide on at least one test case first: only decided test cases are on the record.")
    try:
        url = github.comment_on_pr(*state["repo"], state["number"], report(state)[:60000])
    except github.GithubError as exc:
        raise QAError(str(exc)) from None
    state["posted_url"] = url
    save(state)
    return url
