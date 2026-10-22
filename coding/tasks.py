"""The tasks the Engineering Manager wrote, ready to be picked for coding.

Plain words: every saved run (runs/<id>/session.json) that got as far as a release plan holds
epic -> stories -> tasks. This lists those tasks, with the Jira key when the run created tickets
(runs/<id>/jira_created.json), and builds the briefing the coding agent works from.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

KB_CHARS = 8000        # at most this much of a referenced knowledge-base page goes into the briefing


@dataclass
class CodingTask:
    run_id: str
    story_id: str
    index: int                                  # 1-based position inside the story
    title: str
    description: str
    agent: str                                  # the agent the manager suggested (may be needs_human)
    story: Dict[str, Any] = field(default_factory=dict)
    assignment: Dict[str, Any] = field(default_factory=dict)
    epic_title: str = ""
    jira_key: Optional[str] = None
    subtasks: List[Dict[str, str]] = field(default_factory=list)       # the manager's subtasks: title + description

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.story_id}/T{self.index}"

    @property
    def label(self) -> str:
        jira = f"{self.jira_key} · " if self.jira_key else ""
        return f"{jira}{self.story_id}/T{self.index} · {self.title} [{self.agent}]"


def _plans(obj: Any, found: List[Dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        if obj.get("stories") and isinstance(obj["stories"], list) and "epic" in obj:
            found.append(obj)
        for value in obj.values():
            _plans(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _plans(value, found)


def _find_plan(obj: Any) -> Optional[Dict[str, Any]]:
    """The release plan inside a saved session. A session also holds look-alikes (the Jira summary has stories and
    tasks too, but no assignments), so the plan whose tasks carry the most assignments wins; the earliest on a tie (the real plan is saved before its summaries)."""
    found: List[Dict[str, Any]] = []
    _plans(obj, found)

    def assigned(plan: Dict[str, Any]) -> int:
        return sum(1 for s in plan["stories"] for t in s.get("tasks", []) if (t or {}).get("assignment"))

    best = None
    for plan in found:
        if best is None or assigned(plan) > assigned(best):
            best = plan
    return best


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _subtasks(raw: Any) -> List[Dict[str, str]]:
    """The manager's subtasks as {title, description}. A plain string (as in Jira summaries) is a title only."""
    out = []
    for st in raw if isinstance(raw, list) else []:
        title, desc = (st.get("title"), st.get("description")) if isinstance(st, dict) else (st, "")
        if str(title or "").strip():
            out.append({"title": str(title).strip(), "description": str(desc or "").strip()})
    return out


def tasks_of_plan(plan: Dict[str, Any], run_id: str, created: Optional[Dict[str, str]] = None) -> List[CodingTask]:
    """The tasks of one release plan; `created` maps 'S1/T1' to its Jira key."""
    created = created or {}
    out: List[CodingTask] = []
    for story in plan.get("stories", []):
        sid = story.get("id", "")
        for i, task in enumerate(story.get("tasks", []), start=1):
            assignment = task.get("assignment") or {}
            out.append(CodingTask(
                run_id=run_id, story_id=sid, index=i, title=task.get("title", ""),
                description=task.get("description", ""), agent=assignment.get("agent", "needs_human"),
                story={k: v for k, v in story.items() if k != "tasks"}, assignment=assignment,
                epic_title=(plan.get("epic") or {}).get("title", ""), jira_key=created.get(f"{sid}/T{i}"),
                subtasks=_subtasks(task.get("subtasks"))))
    return out


def jira_keys(run_dir: Path) -> Dict[str, str]:
    return (_read_json(run_dir / "jira_created.json") or {}).get("created", {})


def tasks_of_run(run_dir: Path) -> List[CodingTask]:
    session = _read_json(run_dir / "session.json")
    plan = _find_plan(session) if session else None
    return tasks_of_plan(plan, run_dir.name, jira_keys(run_dir)) if plan else []


def list_tasks(runs_root: Optional[Path] = None) -> List[CodingTask]:
    """Tasks of every saved run, newest run first."""
    root = runs_root or config.RUNS_ROOT
    if not root.is_dir():
        return []
    runs = sorted((p for p in root.iterdir() if (p / "session.json").is_file()), key=lambda p: p.name, reverse=True)
    return [t for run in runs for t in tasks_of_run(run)]


def _kb_text(doc: str, sections: List[str]) -> str:
    path = config.KNOWLEDGE_BASES_ROOT / f"{doc}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[:KB_CHARS]


def briefing(task: CodingTask) -> str:
    """Everything the coding agent is told about the task."""
    a = task.assignment
    out = a.get("output") or {}
    lines = [
        f"TASK: {task.title}",
        task.description,
        "",
        f"STORY {task.story_id}: {task.story.get('title', '')}",
        task.story.get("description", ""),
    ]
    if task.story.get("acceptance_criteria"):
        lines += ["", "Story acceptance criteria:"] + [f"- {c}" for c in task.story["acceptance_criteria"]]
    if out.get("summary"):
        lines += ["", f"Expected deliverable: {out['summary']}"]
    if out.get("paths"):
        lines += ["Planned paths (a guide, not a limit): " + ", ".join(out["paths"])]
    if out.get("done_checks"):
        lines += ["", "The task is done when:"] + [f"- {c}" for c in out["done_checks"]]
    if a.get("assumption"):
        lines += ["", f"Manager's assumption: {a['assumption']}"]
    for ref in a.get("kb_refs") or []:
        text = _kb_text(ref.get("doc", ""), ref.get("sections") or [])
        if text:
            lines += ["", f"KNOWLEDGE BASE ({ref['doc']}; sections of interest: {', '.join(ref.get('sections') or [])}):", text]
    return "\n".join(lines).strip()
