"""
Jira-style epic > stories > tasks > subtasks, written as local mock
artifacts -- the same swap-for-a-real-integration pattern as
ai_native_sdlc's tools/issue_tracker.py: a real Jira integration would
implement the same write_release(...) signature and no caller would need
to change.

No Jira connector is authorized in this environment, so this is the
only implementation for now.

Plain words: stands in for a real Jira. Instead of creating issues on a
website, it saves the epic (the big umbrella item), its stories, each
story's tasks and each task's subtasks as files in a folder - for every
issue a readable .md version and a data .json version.
"""

import json
import re
from pathlib import Path
from typing import Dict, List

from agents import catalog


def _slugify(text: str, max_length: int = 40) -> str:
    # Make text safe for file names/IDs: lowercase, only letters/digits,
    # words joined by "-", and cut to a maximum length.
    # e.g. "User Service!" -> "user-service"
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "item")[:max_length]


# Jira's own priority icon convention, reused here so the mock reads
# like an actual Jira issue list rather than plain text.
_PRIORITY_ICON = {
    "Highest": "⬆️",
    "High": "🔺",
    "Medium": "➖",
    "Low": "🔽",
    "Lowest": "⬇️",
}

# Jira-style type icons used in the markdown pages.
_TYPE_ICON = {"Epic": "🟣", "Story": "🟩", "Task": "🟦", "Sub-task": "◽"}


def _story_md(record: dict, task_records: List[dict]) -> str:
    """Field layout matches a real Jira Story's issue view: a details
    panel of standard Jira fields, using Jira's own relationship phrasing
    ("is blocked by") for links, then Description and Acceptance Criteria
    as a checklist, then the child Tasks (each with its Subtasks)."""
    # Pick the icon for this story's priority.
    icon = _PRIORITY_ICON.get(record["priority"], "")
    # Text listing the stories that must finish first (or "(none)").
    blocked_by_line = ", ".join(record["blocked_by"]) if record["blocked_by"] else "(none)"
    # Build the document line by line: title, a details table, the
    # description, then the acceptance criteria as a tick-box checklist.
    lines = [
        f"# {record['ticket_id']}: {record['title']}",
        "",
        "## Details",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Type | {_TYPE_ICON['Story']} Story |",
        f"| Status | {record['status']} |",
        f"| Priority | {icon} {record['priority']} |",
        f"| Story Points | {record['story_points']} |",
        f"| Epic Link | {record['epic_id']} |",
        f"| Component/s | {record['component']} |",
        f"| Labels | {', '.join(record['labels']) or '(none)'} |",
        "| Sprint | (unassigned) |",
        "| Assignee | (unassigned) |",
        "| Reporter | Claude SDLC Architect |",
        f"| Linked Issues | is blocked by: {blocked_by_line} |",
        "",
        "## Description",
        "",
        record["description"],
        "",
        "## Acceptance Criteria",
        "",
    ] + [f"- [ ] {c}" for c in record["acceptance_criteria"]]

    # Then every task of the story, with its subtasks as a checklist.
    lines += ["", f"## Tasks ({len(task_records)})", ""]
    for t in task_records:
        lines.append(f"- {_TYPE_ICON['Task']} [{t['ticket_id']}]({t['ticket_id']}.md) {t['title']}")
        for st in t["subtasks"]:
            lines.append(f"  - [ ] {_TYPE_ICON['Sub-task']} [{st['ticket_id']}]({st['ticket_id']}.md) {st['title']}")
    # Join the lines with newlines into one block of text.
    return "\n".join(lines) + "\n"


def _child_md(record: dict, kind: str, children: List[dict]) -> str:
    """A Task or Sub-task page: a small details panel (type, status, which
    issue it belongs to), the description, and - for a Task - its
    Subtasks."""
    lines = [
        f"# {record['ticket_id']}: {record['title']}",
        "",
        "## Details",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Type | {_TYPE_ICON[kind]} {kind} |",
        f"| Status | {record['status']} |",
        f"| Parent | [{record['parent_id']}]({record['parent_id']}.md) |",
        f"| Component/s | {record['component']} |",
        "| Assignee | (unassigned) |",
        "| Reporter | Claude SDLC Architect |",
        "",
        "## Description",
        "",
        record["description"] or "(no further detail)",
    ]
    assignment = catalog.assignment_lines(record.get("assignment") or {})
    if assignment:
        lines += ["", "## Assignment", ""] + [f"- {line}" for line in assignment]
    if children:
        lines += ["", f"## Subtasks ({len(children)})", ""]
        lines += [f"- [ ] [{c['ticket_id']}]({c['ticket_id']}.md) {c['title']}" for c in children]
    return "\n".join(lines) + "\n"


def _epic_md(epic_record: dict, story_records: List[dict]) -> str:
    """Mirrors a real Jira Epic's issue view: its own details panel,
    then the child Stories listed the way Jira's "Epic children" panel
    shows them -- key, type icon, summary, status, priority."""
    # Header part of the epic page: title, details table, description.
    lines = [
        f"# {epic_record['epic_id']}: {epic_record['title']}",
        "",
        "## Details",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Type | {_TYPE_ICON['Epic']} Epic |",
        f"| Status | {epic_record['status']} |",
        "| Reporter | Claude SDLC Architect |",
        "",
        "## Description",
        "",
        epic_record["description"],
        "",
        f"## Child Stories ({len(story_records)})",
        "",
    ]
    # Then one bullet per child story, linking to that story's own page.
    for s in story_records:
        icon = _PRIORITY_ICON.get(s["priority"], "")
        lines.append(
            f"- {_TYPE_ICON['Story']} [{s['ticket_id']}]({s['ticket_id']}.md) {s['title']} "
            f"-- {s['status']}, {icon} {s['priority']}, {s['story_points']} pts "
            f"({s['component']}; {len(s['tasks'])} task(s))"
        )
    return "\n".join(lines) + "\n"


def _save(output_dir: Path, record: dict, markdown: str) -> None:
    # Save machine-readable (.json) and human-readable (.md) versions of
    # one issue, both named after its ID.
    (output_dir / f"{record['ticket_id']}.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )
    (output_dir / f"{record['ticket_id']}.md").write_text(markdown, encoding="utf-8")


def write_release(
    output_dir: Path,
    epic: dict,
    stories: List[dict],
    run_id: str,
) -> dict:
    """Save the epic and everything under it as files in `output_dir`
    and return a summary of what was written (ids, file paths and the
    full story > task > subtask tree)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # The epic's ID, built from the run ID, e.g. "ARCH-20260101-1200".
    epic_id = f"ARCH-{run_id[:12]}"

    # Assign every story an issue ID up front, so each story's blocked_by
    # (which uses the agent's own ids like "S1") can point at the other
    # stories' real issue IDs.
    story_keys = {
        story["id"]: f"{epic_id}-{_slugify(str(story['id']), 12).upper()}" for story in stories
    }

    story_records = []
    tree = []  # the same data with tasks/subtasks nested, for display

    for story in stories:
        story_key = story_keys[story["id"]]
        component = story["component"]

        # Create the story's tasks, and each task's subtasks, first, so the
        # story record can list their IDs.
        task_records = []
        for t_index, task in enumerate(story["tasks"], start=1):
            task_key = f"{story_key}-T{t_index}"
            subtask_records = []
            for s_index, sub in enumerate(task["subtasks"], start=1):
                sub_record = {
                    "ticket_id": f"{task_key}-ST{s_index}",
                    "parent_id": task_key,
                    "issue_type": "Sub-task",
                    "title": sub["title"],
                    "description": sub.get("description", ""),
                    "component": component,
                    "status": "TO DO",
                }
                subtask_records.append(sub_record)
                _save(output_dir, sub_record, _child_md(sub_record, "Sub-task", []))

            task_record = {
                "ticket_id": task_key,
                "parent_id": story_key,
                "issue_type": "Task",
                "title": task["title"],
                "description": task.get("description", ""),
                "component": component,
                "status": "TO DO",
                "subtasks": [s["ticket_id"] for s in subtask_records],
                "assignment": task.get("assignment") or {},
            }
            task_records.append((task_record, subtask_records))
            _save(output_dir, task_record, _child_md(task_record, "Task", subtask_records))

        # The story's data, with sensible defaults for missing fields.
        record = {
            "ticket_id": story_key,
            "epic_id": epic_id,
            "component": component,
            "title": story["title"],
            "description": story["description"],
            "acceptance_criteria": story.get("acceptance_criteria", []),
            # Convert "blocked by S1" into "blocked by <S1's real issue id>".
            "blocked_by": [story_keys[b] for b in story.get("blocked_by", []) if b in story_keys],
            "team": story.get("team", ""),
            "status": "TO DO",
            "issue_type": "Story",
            "priority": story.get("priority", "Medium"),
            "story_points": story.get("story_points", 0),
            "labels": story.get("labels", []),
            "tasks": [t["ticket_id"] for t, _ in task_records],
        }
        story_records.append(record)
        _save(
            output_dir,
            record,
            _story_md(
                record,
                [{**t, "subtasks": subs} for t, subs in task_records],
            ),
        )

        tree.append(
            {
                **record,
                "path": str(output_dir / f"{story_key}.md"),
                "tasks": [{**t, "subtasks": subs} for t, subs in task_records],
            }
        )

    # Now write the epic itself, listing all of its stories.
    epic_record = {
        "epic_id": epic_id,
        "title": epic["title"],
        "description": epic["description"],
        "status": "IN PROGRESS",
        "stories": [s["ticket_id"] for s in story_records],
    }
    (output_dir / f"{epic_id}.json").write_text(json.dumps(epic_record, indent=2), encoding="utf-8")
    (output_dir / f"{epic_id}.md").write_text(_epic_md(epic_record, story_records), encoding="utf-8")

    # Tell the caller what was created and where.
    return {
        "epic_id": epic_id,
        "epic_path": str(output_dir / f"{epic_id}.md"),
        "stories": tree,
        "counts": {
            "stories": len(tree),
            "tasks": sum(len(s["tasks"]) for s in tree),
            "subtasks": sum(len(t["subtasks"]) for s in tree for t in s["tasks"]),
        },
    }
