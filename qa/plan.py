"""The test plan the Quality Planner wrote, and which merged pull request it belongs to.

Plain words: every saved run (runs/<id>/session.json) that got as far as the quality stage holds a test plan. A coding job remembers the
run its task came from, the repository and the branch of its pull request, so a merged pull request can be matched back to its plan.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

PRIORITY = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
_cache: Dict[str, tuple] = {}                                  # run id -> (file modification time, parsed session)


def _session(run_id: str) -> Optional[Dict[str, Any]]:
    path = config.RUNS_ROOT / run_id / "session.json"
    try:
        mtime = path.stat().st_mtime
        if _cache.get(run_id, (None,))[0] != mtime:
            _cache[run_id] = (mtime, json.loads(path.read_text(encoding="utf-8")))
        return _cache[run_id][1]
    except (OSError, ValueError):
        return None


def _case(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": str(raw.get("id") or "").strip(), "title": str(raw.get("title") or "").strip(), "component": str(raw.get("component") or "").strip(),
            "type": str(raw.get("type") or "").strip(), "priority": str(raw.get("priority") or "Medium").strip(),
            "steps": [str(s) for s in raw.get("steps") or []], "expected_result": str(raw.get("expected_result") or "").strip(),
            "requirement_refs": [str(r) for r in raw.get("requirement_refs") or []], "preconditions": str(raw.get("preconditions") or "").strip(),
            "test_data": str(raw.get("test_data") or "").strip()}


def load_plan(run_id: str) -> Optional[Dict[str, Any]]:
    """{'run_id','title','summary','test_cases','acceptance_criteria'} for a run, or None if it has no test plan."""
    s = _session(run_id)
    ps = (s or {}).get("pipeline_state") or {}
    plan = ps.get("quality_plan")
    if not plan or not plan.get("test_cases"):
        return None
    req = ps.get("requirement") or {}
    cases = [c for c in (_case(t) for t in plan["test_cases"] if isinstance(t, dict)) if c["id"]]
    return {"run_id": run_id, "title": req.get("title") or run_id, "summary": plan.get("summary") or "", "test_cases": cases,
            "acceptance_criteria": [{"id": f"AC-{i}", "text": str(t)} for i, t in enumerate(req.get("acceptance_criteria") or [], start=1)]}


def runs_with_plans() -> List[Dict[str, Any]]:
    """Every saved run that has a test plan, newest first: [{'run_id','title','cases'}]."""
    root = config.RUNS_ROOT
    out = []
    if root.is_dir():
        for p in sorted((p for p in root.iterdir() if (p / "session.json").is_file()), key=lambda p: p.name, reverse=True):
            plan = load_plan(p.name)
            if plan:
                out.append({"run_id": p.name, "title": plan["title"], "cases": len(plan["test_cases"])})
    return out


def find_run_for_pr(owner: str, name: str, branch: str) -> Optional[str]:
    """The run whose coding job opened a pull request from this branch of this repository (newest job first), or None."""
    from coding import flow
    from coding import workspace as ws
    for job in flow.list_jobs():
        try:
            found = ws.github_repo(job.d["ws"].get("source", "") or "")
            if found and (found[0].lower(), found[1].lower()) == (owner.lower(), name.lower()) and job.workspace.branch == branch:
                run_id = job.d["task"]["run_id"]
                if load_plan(run_id):
                    return run_id
        except (KeyError, OSError, ws.GitError):
            continue
    return None
