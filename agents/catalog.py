"""Who can do the work, and what a finished task hands back.

Plain words: the Engineering Manager (agents/release.py) gives every task an
`assignment`: which specialist agent should do it, which of that agent's skills it
needs, which parts of the knowledge base it should read, and what the output is (for
most coding work, a pull request). This module holds the list of agents the manager
may choose from, the kinds of output, and the checks that reject a bad assignment
(an unknown agent, a document or heading that does not exist, a pull request with no
branch...). The checks run in code, so a mistake costs one corrected answer and not a
human's time. No AI is used here.

Token care: `prompt_block()` and `kb_index()` are the only things added to the manager's
prompt - one short line per agent, and headings (not text) of the few documents that matter.
"""

import re
from typing import Any, Dict, List, Optional, Set

import config
import token_log
from knowledge_base import loader

NEEDS_HUMAN = "needs_human"

# id -> (use when, file extensions this agent works in). The extensions let the code spot a
# task whose files obviously belong to another agent.
ROLES: Dict[str, Dict[str, Any]] = {
    "coding_python": {"use": "Python services, APIs, CLIs, libraries (FastAPI/Flask/Django, asyncio)",
                      "ext": {".py", ".toml", ".cfg", ".txt", ".ini"}},
    "coding_java": {"use": "JVM backends (Spring/Quarkus/Micronaut, JPA, Maven/Gradle)",
                    "ext": {".java", ".kt", ".scala", ".xml", ".gradle", ".properties", ".yml", ".yaml", ".sql"}},
    "coding_web": {"use": "web frontends (React/Next/Vue/Svelte/Angular, TypeScript/JS, CSS)",
                   "ext": {".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte", ".css", ".scss", ".html", ".json"}},
    "coding_android": {"use": "Android apps (Kotlin/Java, Compose/XML, Gradle)",
                       "ext": {".kt", ".java", ".xml", ".gradle", ".kts", ".pro"}},
    "coding_ios": {"use": "iOS apps (Swift/ObjC, SwiftUI/UIKit, SPM)",
                   "ext": {".swift", ".m", ".h", ".storyboard", ".xib", ".plist", ".xcstrings"}},
    "coding_data_science": {"use": "analysis, ML/DL models, forecasting, experiments, notebooks",
                            "ext": {".py", ".ipynb", ".yaml", ".yml", ".toml", ".csv"}},
    "coding_data_engineering": {"use": "SQL/dbt, pipelines, Spark, streaming, warehouses, data quality",
                                "ext": {".sql", ".py", ".yml", ".yaml", ".scala", ".json", ".toml"}},
}
VALID_AGENTS = set(ROLES) | {NEEDS_HUMAN}

# Output types. Every output states what is delivered and how "done" is checked; a pull
# request also names the repo, branch and the paths it may touch.
OUTPUT_TYPES = {"pull_request", "document", "report", "config_change", "decision"}
_CODING_OUTPUTS = {"pull_request", "document", "report", "config_change"}


def skills_of(agent: str) -> Set[str]:
    """The separate skills an agent can be given. Each agent is one skill file now, so there are none to choose."""
    return set()


def prompt_block() -> str:
    """The agent list, one short line each, for the manager's prompt."""
    lines = [f"- {a}: {r['use']}" for a, r in ROLES.items()]
    lines.append(f"- {NEEDS_HUMAN}: nothing above fits (infrastructure/DevOps, QA automation, docs-only, "
                 "unsupported stack, or a business decision)")
    return "\n".join(lines)


# ---------- knowledge-base index ----------

def _headings(doc: loader.ApplicationDoc, limit: int = 10) -> List[str]:
    found = [re.sub(r"[#*`]", "", line).strip() for line in doc.content.splitlines() if re.match(r"#{1,3}\s", line)]
    return [h[:48] for h in found if h][:limit]


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def kb_index(applications: List[loader.ApplicationDoc], focus: Set[str] = frozenset()) -> str:
    """The applications a task may reference: name and one-line purpose (and any section headings its text has).
    Capped, so it stays a few hundred tokens."""
    if not applications:
        return "(none: this is a new build, so tasks have no knowledge-base references)"
    lines = []
    for d in applications[:12]:
        heads = _headings(d)
        lines.append(f"- {d.name}: {(d.purpose or '')[:110]}" + (f" | sections: {' | '.join(heads)}" if heads else ""))
    return "\n".join(lines)


# ---------- checks ----------

def _heading_ok(section: str, headings: List[str]) -> bool:
    s = _norm(section)
    return bool(s) and any(s in _norm(h) or _norm(h) in s for h in headings)


def check_assignment(a: Any, where: str, applications: Optional[List[loader.ApplicationDoc]] = None,
                     all_headings: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Everything wrong with one assignment, as short messages (empty = fine)."""
    if not isinstance(a, dict):
        return [f"{where}: assignment is missing (an object with agent, skills, kb_refs, output)."]
    errs: List[str] = []
    agent = a.get("agent")
    if agent not in VALID_AGENTS:
        return [f"{where}: agent {agent!r} is not one of {sorted(VALID_AGENTS)}."]

    skills = a.get("skills", [])
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        errs.append(f"{where}: skills must be a list of skill names.")
    else:
        bad = [s for s in skills if s not in skills_of(agent)]
        if bad:
            errs.append(f"{where}: {agent} has no skill(s) {bad}; its skills are {sorted(skills_of(agent))}.")

    refs = a.get("kb_refs", [])
    if not isinstance(refs, list):
        errs.append(f"{where}: kb_refs must be a list of {{doc, sections}}.")
    else:
        known = {d.name: d for d in applications or []}
        for r in refs:
            doc = r.get("doc") if isinstance(r, dict) else None
            if doc not in known:
                errs.append(f"{where}: kb_refs names {doc!r}, which is not in the knowledge base index.")
                continue
            heads = (all_headings or {}).get(doc) or _headings(known[doc], limit=60)
            for sec in (r.get("sections") or []):
                if not _heading_ok(str(sec), heads):
                    errs.append(f"{where}: {doc} has no heading like {sec!r}.")

    out = a.get("output")
    if not isinstance(out, dict) or out.get("type") not in OUTPUT_TYPES:
        errs.append(f"{where}: output.type must be one of {sorted(OUTPUT_TYPES)}.")
        return errs
    if out["type"] == "decision" and agent != NEEDS_HUMAN:
        errs.append(f"{where}: a decision is made by a person - use needs_human, or pick another output type.")
    if agent in ROLES and out["type"] not in _CODING_OUTPUTS:
        errs.append(f"{where}: {agent} cannot produce a {out['type']}.")
    if not str(out.get("summary") or "").strip():
        errs.append(f"{where}: output.summary (what is delivered) is empty.")
    checks = out.get("done_checks")
    if not isinstance(checks, list) or not [c for c in checks if str(c).strip()]:
        errs.append(f"{where}: output.done_checks needs at least one checkable condition.")
    if out["type"] == "pull_request":
        for key in ("repo", "branch"):
            if not str(out.get(key) or "").strip():
                errs.append(f"{where}: pull_request output needs {key}.")
        paths = out.get("paths")
        if not isinstance(paths, list) or not [p for p in paths if str(p).strip()]:
            errs.append(f"{where}: pull_request output needs paths (the files or folders it may touch).")
        elif agent in ROLES:
            exts = {m.group(0).lower() for p in paths if (m := re.search(r"\.[A-Za-z0-9]+$", str(p)))}
            if exts and not exts & ROLES[agent]["ext"]:
                errs.append(f"{where}: paths {sorted(exts)} do not belong to {agent}; pick the agent that owns them.")
    if agent == NEEDS_HUMAN and not str(a.get("reason") or "").strip():
        errs.append(f"{where}: needs_human must say why in `reason`.")
    return errs


def check_plan(stories: List[Dict[str, Any]], applications: Optional[List[loader.ApplicationDoc]] = None,
               require: bool = True) -> List[str]:
    """All assignment problems in a plan, collected so one retry can fix them together."""
    errs: List[str] = []
    known = {d.name for d in applications or []}
    heads = {d.name: _headings(d, limit=60) for d in applications or []}
    for story in stories:
        refd: Set[str] = set()
        for i, task in enumerate(story.get("tasks", []), start=1):
            where = f"{story.get('id')}/T{i}"
            a = task.get("assignment")
            if a is None and not require:
                continue
            errs += check_assignment(a, where, applications, heads)
            for r in (a or {}).get("kb_refs", []) if isinstance(a, dict) else []:
                if isinstance(r, dict):
                    refd.add(r.get("doc"))
        # A story about an application that HAS a document must read it somewhere.
        comp = story.get("component")
        if require and comp in known and comp not in refd and story.get("tasks"):
            errs.append(f"{story.get('id')}: no task references the knowledge-base document {comp!r}.")
    return errs


# ---------- display ----------

def assignment_lines(a: Dict[str, Any]) -> List[str]:
    """The assignment as plain lines (for ticket pages, Jira descriptions and the screen)."""
    if not isinstance(a, dict) or not a:
        return []
    out = a.get("output") or {}
    refs = "; ".join(
        f"{r.get('doc')}" + (f" ({', '.join(r.get('sections') or [])})" if r.get("sections") else "")
        for r in a.get("kb_refs") or [] if isinstance(r, dict)) or "none"
    lines = [f"Agent: {a.get('agent')}" + (f" - {a['reason']}" if a.get("reason") else ""),
             f"Skills: {', '.join(a.get('skills') or []) or 'all of its skills'}",
             f"Knowledge base: {refs}",
             f"Output: {out.get('type')} - {out.get('summary', '')}"]
    if out.get("type") == "pull_request":
        lines.append(f"Pull request: repo {out.get('repo')}, branch {out.get('branch')}, "
                     f"may touch {', '.join(out.get('paths') or [])}")
    if out.get("done_checks"):
        lines.append("Done when: " + "; ".join(str(c) for c in out["done_checks"]))
    if a.get("assumption"):
        lines.append(f"Assumption: {a['assumption']}")
    if a.get("needs_review"):
        lines.append("NEEDS REVIEW" + (f" - open question: {a['open_question']}" if a.get("open_question") else ""))
    return lines


def summarise(stories: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How many tasks each agent has, and which need a person (computed, not asked of the AI)."""
    counts: Dict[str, int] = {}
    human: List[str] = []
    review: List[str] = []
    for s in stories:
        for i, t in enumerate(s.get("tasks", []), start=1):
            agent = (t.get("assignment") or {}).get("agent")
            if agent:
                counts[agent] = counts.get(agent, 0) + 1
                if agent == NEEDS_HUMAN:
                    human.append(f"{s.get('id')}/T{i}")
                if (t.get("assignment") or {}).get("needs_review"):
                    review.append(f"{s.get('id')}/T{i}")
    return {"tasks_per_agent": counts, "needs_human": human, "needs_review": review}


def log_context(block: str, index: str) -> None:
    token_log.record("release_context", "Release Agent",
                     catalog_tokens=token_log.est_tokens(block), kb_index_tokens=token_log.est_tokens(index))
