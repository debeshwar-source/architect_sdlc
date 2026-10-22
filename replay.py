"""Re-run one stage of a saved run with the CURRENT settings and compare it with what that run produced.

    python3 replay.py RUN_ID STAGE                     # STAGE: requirement | design | quality | release

Plain words: the safety check for every token-saving change. It feeds a stage the same inputs it had
in a finished run, lets it answer again, and prints (1) how similar the new answer is to the saved one
and (2) how many tokens the call used. If a change saves tokens but the answers drift, don't keep it.
This calls the real AI, so it uses real tokens: run it deliberately.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Set

import config
import token_report

STAGES = ("requirement", "design", "quality", "release")
_STOP = set("the a an and or of to in for on with by is are be as at it that this from into".split())


def jaccard(a: Set[Any], b: Set[Any]) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def _words(text: str) -> Set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in _STOP}


def _names(items, key="name") -> Set[str]:
    return {i.get(key) for i in items or [] if isinstance(i, dict) and i.get(key)}


def compare(stage: str, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """How alike two answers are, part by part (1.0 = identical on that part), and their mean."""
    parts: Dict[str, float] = {}
    notes: List[str] = []
    if stage == "requirement":
        oc, nc = old.get("acceptance_criteria") or [], new.get("acceptance_criteria") or []
        covered = sum(1 for o in oc if any(len(_words(o) & _words(n)) / max(len(_words(o) | _words(n)), 1) >= 0.35 for n in nc))
        parts["criteria_covered"] = covered / len(oc) if oc else 1.0
        parts["build_mode"] = float(old.get("build_mode") == new.get("build_mode"))
        parts["scope_words"] = jaccard(_words(old.get("scope_summary", "")), _words(new.get("scope_summary", "")))
        notes.append(f"criteria {len(oc)} -> {len(nc)}; out-of-scope {len(old.get('out_of_scope') or [])} -> {len(new.get('out_of_scope') or [])}")
    elif stage == "design":
        parts["change_required"] = float(old.get("change_required") == new.get("change_required"))
        parts["affected"] = jaccard(_names(old.get("affected_applications")), _names(new.get("affected_applications")))
        parts["new_apps"] = jaccard(_names(old.get("new_applications_needed")), _names(new.get("new_applications_needed")))
        parts["components"] = jaccard(_names(old.get("components")), _names(new.get("components")))
        edge = lambda d: {(c.get("from"), c.get("to")) for c in d.get("connections") or [] if isinstance(c, dict)}  # noqa: E731
        parts["connections"] = jaccard(edge(old), edge(new))
        notes.append(f"components {len(old.get('components') or [])} -> {len(new.get('components') or [])}; "
                     f"connections {len(edge(old))} -> {len(edge(new))}; flows {len(old.get('flows') or [])} -> {len(new.get('flows') or [])}")
    elif stage == "quality":
        cases = lambda d: d.get("test_cases") or []  # noqa: E731
        refs = lambda d: {r for c in cases(d) for r in (c.get("requirement_refs") or [])}  # noqa: E731
        parts["criteria_referenced"] = jaccard(refs(old), refs(new))
        parts["components"] = jaccard({c.get("component") for c in cases(old)}, {c.get("component") for c in cases(new)})
        parts["case_count"] = min(len(cases(old)), len(cases(new))) / max(len(cases(old)), len(cases(new)), 1)
        notes.append(f"test cases {len(cases(old))} -> {len(cases(new))}")
    elif stage == "release":
        stories = lambda d: d.get("stories") or []  # noqa: E731
        parts["story_components"] = jaccard({s.get("component") for s in stories(old)}, {s.get("component") for s in stories(new)})
        tasks = lambda d: [t for s in stories(d) for t in s.get("tasks") or []]  # noqa: E731
        parts["task_count"] = min(len(tasks(old)), len(tasks(new))) / max(len(tasks(old)), len(tasks(new)), 1)
        agents = lambda d: sorted((t.get("assignment") or {}).get("agent", "") for t in tasks(d))  # noqa: E731
        parts["agent_mix"] = jaccard(set(agents(old)), set(agents(new)))
        notes.append(f"stories {len(stories(old))} -> {len(stories(new))}; tasks {len(tasks(old))} -> {len(tasks(new))}")
    else:
        raise ValueError(f"unknown stage {stage!r}")
    mean = sum(parts.values()) / len(parts)
    return {"similarity": round(mean, 2), "parts": {k: round(v, 2) for k, v in parts.items()}, "notes": notes}


def load_run(run_id: str, runs_root: Path = None) -> Dict[str, Any]:
    path = (runs_root or config.RUNS_ROOT) / run_id / "session.json"
    return json.loads(path.read_text(encoding="utf-8"))["pipeline_state"]


def replay(run_id: str, stage: str) -> Dict[str, Any]:
    """Run `stage` again on the saved inputs. Returns the new answer, the saved one, their comparison
    and the token summary of the call."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    state = load_run(run_id)
    key = {"requirement": "requirement", "design": "design", "quality": "quality", "release": "release"}[stage]
    old = {"requirement": state.get("requirement"), "design": state.get("design"),
           "quality": state.get("quality_plan"), "release": state.get("release_plan")}[stage]
    if not old:
        raise ValueError(f"run {run_id} has no saved {stage} answer to compare with")

    from knowledge_base import loader
    scratch = bool(state.get("from_scratch") or state.get("force_from_scratch"))
    apps = [] if scratch or not state.get("kb_name") else loader.load_all(loader.kb_path(state["kb_name"]))
    log = config.RUNS_ROOT / "token_log.jsonl"
    mark = len(log.read_text().splitlines()) if log.exists() else 0

    if stage == "requirement":
        from agents import requirement as agent
        new = agent.run(state["requirement_text"], apps, from_scratch=scratch)
    elif stage == "design":
        from agents import design as agent
        new = agent.run(state["requirement"], apps, from_scratch=scratch)
    elif stage == "quality":
        from agents import quality as agent
        new = agent.run(state["requirement"], state["design"])
    else:
        from agents import release as agent
        new = agent.run(state["requirement"], state["design"], quality_plan=state.get("quality_plan"), applications=apps)

    rows = log.read_text().splitlines()[mark:] if log.exists() else []
    summary = token_report.summarise([json.loads(r) for r in rows])
    return {"old": old, "new": new, "comparison": compare(stage, old, new), "tokens": summary}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_id")
    ap.add_argument("stage", choices=STAGES)
    args = ap.parse_args()
    out = replay(args.run_id, args.stage)
    print(json.dumps(out["comparison"], indent=2))
    print(token_report.render(out["tokens"]))
