"""
Jira-style epic + linked tickets, written as local mock artifacts --
same swap-for-a-real-integration pattern as ai_native_sdlc's
tools/issue_tracker.py: a real Jira integration would implement the
same write_release(...) signature and no caller would need to change.

No Jira connector is authorized in this environment, so this is the
only implementation for now.
"""

import json
import re
from pathlib import Path
from typing import Dict, List


def _slugify(text: str, max_length: int = 40) -> str:
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


def _ticket_md(record: dict) -> str:
    """Field layout matches a real Jira Story's issue view: a details
    panel of standard Jira fields (Type/Status/Priority/Story Points/
    Epic Link/Components/Labels/Sprint/Assignee/Reporter), using
    Jira's own relationship phrasing ("is blocked by") for links, then
    Description and Acceptance Criteria as a checklist -- the same
    shape and terminology an export of a real Jira Story would have."""
    icon = _PRIORITY_ICON.get(record["priority"], "")
    blocked_by_line = (
        ", ".join(record["blocked_by"]) if record["blocked_by"] else "(none)"
    )
    lines = [
        f"# {record['ticket_id']}: {record['title']}",
        "",
        "## Details",
        "",
        f"| Field | Value |",
        f"|---|---|",
        f"| Type | 🟩 {record['issue_type']} |",
        f"| Status | {record['status']} |",
        f"| Priority | {icon} {record['priority']} |",
        f"| Story Points | {record['story_points']} |",
        f"| Epic Link | {record['epic_id']} |",
        f"| Component/s | {record['application']} |",
        f"| Labels | {', '.join(record['labels']) or '(none)'} |",
        f"| Sprint | (unassigned) |",
        f"| Assignee | (unassigned) |",
        f"| Reporter | Claude SDLC Architect |",
        f"| Linked Issues | is blocked by: {blocked_by_line} |",
        "",
        "## Description",
        "",
        record["description"],
        "",
        "## Acceptance Criteria",
        "",
    ] + [f"- [ ] {c}" for c in record["acceptance_criteria"]]
    return "\n".join(lines) + "\n"


def _epic_md(epic_record: dict, ticket_records: List[dict]) -> str:
    """Mirrors a real Jira Epic's issue view: its own details panel,
    then the child Stories listed the way Jira's "Epic children" panel
    shows them -- key, type icon, summary, status, priority."""
    lines = [
        f"# {epic_record['epic_id']}: {epic_record['title']}",
        "",
        "## Details",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Type | 🟣 Epic |",
        f"| Status | {epic_record['status']} |",
        f"| Reporter | Claude SDLC Architect |",
        "",
        "## Description",
        "",
        epic_record["description"],
        "",
        f"## Child Stories ({len(ticket_records)})",
        "",
    ]
    for t in ticket_records:
        icon = _PRIORITY_ICON.get(t["priority"], "")
        lines.append(
            f"- 🟩 [{t['ticket_id']}]({t['ticket_id']}.md) {t['title']} "
            f"-- {t['status']}, {icon} {t['priority']}, {t['story_points']} pts "
            f"({t['application']})"
        )
    return "\n".join(lines) + "\n"


def write_release(
    output_dir: Path,
    epic: dict,
    tickets: List[dict],
    run_id: str,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)

    epic_id = f"ARCH-{run_id[:12]}"

    # Assign every application a ticket id up front, so each ticket's
    # blocked_by can reference the other tickets' real ids rather than
    # just application names.
    ticket_ids_by_app = {
        ticket["application"]: f"{epic_id}-{_slugify(ticket['application'], 20).upper()}"
        for ticket in tickets
    }

    ticket_records = []

    for ticket in tickets:
        ticket_id = ticket_ids_by_app[ticket["application"]]
        blocked_by_ids = [
            ticket_ids_by_app[app]
            for app in ticket.get("blocked_by", [])
            if app in ticket_ids_by_app
        ]

        record = {
            "ticket_id": ticket_id,
            "epic_id": epic_id,
            "application": ticket["application"],
            "title": ticket["title"],
            "description": ticket["description"],
            "acceptance_criteria": ticket.get("acceptance_criteria", []),
            "blocked_by": blocked_by_ids,
            "team": ticket.get("team", ""),
            "status": "TO DO",
            "issue_type": "Story",
            "priority": ticket.get("priority", "Medium"),
            "story_points": ticket.get("story_points", 0),
            "labels": ticket.get("labels", []),
            "components": [ticket["application"]],
        }
        ticket_records.append(record)

        (output_dir / f"{ticket_id}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        (output_dir / f"{ticket_id}.md").write_text(_ticket_md(record), encoding="utf-8")

    epic_record = {
        "epic_id": epic_id,
        "title": epic["title"],
        "description": epic["description"],
        "status": "IN PROGRESS",
        "tickets": [t["ticket_id"] for t in ticket_records],
    }
    (output_dir / f"{epic_id}.json").write_text(
        json.dumps(epic_record, indent=2), encoding="utf-8"
    )
    (output_dir / f"{epic_id}.md").write_text(
        _epic_md(epic_record, ticket_records), encoding="utf-8"
    )

    return {
        "epic_id": epic_id,
        "epic_path": str(output_dir / f"{epic_id}.md"),
        "tickets": [
            {**record, "path": str(output_dir / f"{record['ticket_id']}.md")}
            for record in ticket_records
        ],
    }
