"""
Creates the release backlog in a REAL Jira project, over Jira's REST API.

Plain words: tools/issue_tracker.py saves the epic / stories / tasks as local
files (a pretend Jira). This module does the real thing - it creates the same
items in your actual Jira - but ONLY when the pipeline calls it after a human
has approved (see jira_publish_node in orchestrator/graph.py). It never runs
on its own, and nothing here talks to the network until publish() is called.

Settings come from the environment / the git-ignored .env file:

    JIRA_BASE_URL     https://yourcompany.atlassian.net   (or your server)
    JIRA_EMAIL        account email (Jira Cloud). Leave empty on Data Center.
    JIRA_API_TOKEN    API token (Cloud) or personal access token (Data Center)
    JIRA_PROJECT_KEY  e.g. ARCH

How our backlog is mapped onto Jira
-----------------------------------
Jira only has three standard levels (Epic > Story > Sub-task), while the
pipeline produces four (Epic > Story > Task > Subtask). So:

    our Epic     ->  Jira Epic
    our Story    ->  Jira Story, placed under the Epic
    our Task     ->  Jira Sub-task of its Story
    our Subtask  ->  a "Steps" bullet list inside that Sub-task's description

(This one mapping lives in publish(); change it there if your Jira has a
custom hierarchy.) "blocked by" becomes a Jira "Blocks" link between stories.
The component is added as a label (`component-<name>`) rather than a Jira
Component, because components must already exist and need admin rights to
create.

Choosing what to create
-----------------------
The caller may pass a `selection`: the set of item keys to create ("epic",
"S1" for a story, "S1/T2" for that story's 2nd task). Nothing else is sent.
Keys are stable for a given plan, and progress is recorded against the WHOLE
plan, so publishing a subset now and more later never duplicates anything.
A task needs its story to exist in Jira (selected now, or created earlier);
without the epic, stories are simply created ungrouped.

Safety properties
-----------------
* Idempotent: a progress file (runs/<run_id>/jira_created.json) records every
  issue as it is created. Publishing the same plan again never duplicates
  tickets - after a failure it resumes where it stopped, and after a success
  it just returns the existing tickets. If the plan changed since, it starts
  fresh and warns that the earlier tickets still exist.
* Tolerant of project differences: optional fields (priority, story points,
  labels) that a project's screens do not accept are dropped and reported
  rather than failing the run. A field the project REQUIRES that we cannot
  fill produces a clear error naming it.
* The API token is never logged and is scrubbed from any error text.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

# The REST API version used. v2 takes plain text / wiki-markup descriptions,
# which is simpler than v3's document format and works on Cloud and Data Center.
API = "/rest/api/2"
TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 4          # per request, for rate limits (429) and server hiccups (5xx)
SUMMARY_LIMIT = 250       # Jira rejects summaries longer than 255 characters

MAPPING_NOTE = (
    "Tasks were created as Jira Sub-tasks of their Story; each task's subtasks "
    "are listed as steps in the Sub-task description."
)


class JiraError(Exception):
    """Anything that goes wrong talking to Jira. Carries Jira's own
    per-field `errors` (if any) and how many issues had already been created."""

    def __init__(self, message: str, status: int = 0, errors: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.errors = errors or {}
        self.created_count = 0


# ------------------------------------------------------------
# Settings and the no-network helpers the UI / CLI use
# ------------------------------------------------------------

def _setting(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def missing_settings() -> List[str]:
    """Names of required settings that are not set. (JIRA_EMAIL is only
    needed for Jira Cloud, so it is not required here.)"""
    return [n for n in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY") if not _setting(n)]


def is_configured() -> bool:
    return not missing_settings()


def _normalise_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if re.match(r"^https?://", url) else f"https://{url}"


def plan_keys(plan: Dict[str, Any]) -> List[str]:
    """Every item that could be created, as stable keys: "epic", then for each
    story its id ("S1") followed by its tasks ("S1/T1", "S1/T2"...)."""
    keys = ["epic"]
    for story in plan.get("stories", []):
        keys.append(story["id"])
        keys += [f"{story['id']}/T{i}" for i, _ in enumerate(story.get("tasks", []), start=1)]
    return keys


def preview(plan: Dict[str, Any]) -> Dict[str, Any]:
    """What publishing WOULD do, for the permission prompt. Makes no network
    calls and contains no secret (the token is never included). `items` is the
    list the user ticks from."""
    stories = plan.get("stories", [])
    tasks = [t for s in stories for t in s.get("tasks", [])]
    return {
        "epic_title": (plan.get("epic") or {}).get("title", ""),
        "items": [
            {
                "key": s["id"], "title": s.get("title", ""), "component": s.get("component", ""),
                "priority": s.get("priority", ""),
                "tasks": [{"key": f"{s['id']}/T{i}", "title": t.get("title", "")}
                          for i, t in enumerate(s.get("tasks", []), start=1)],
            }
            for s in stories
        ],
        "configured": is_configured(),
        "missing": missing_settings(),
        "base_url": _normalise_url(_setting("JIRA_BASE_URL")) if _setting("JIRA_BASE_URL") else "",
        "project_key": _setting("JIRA_PROJECT_KEY"),
        "account": _setting("JIRA_EMAIL") or "(token only)",
        "epics": 1,
        "stories": len(stories),
        "sub_tasks": len(tasks),                                   # our tasks
        "steps": sum(len(t.get("subtasks", [])) for t in tasks),   # our subtasks
        "mapping": MAPPING_NOTE,
    }


def from_env(**kwargs) -> "JiraClient":
    """Build a client from the environment. Raises JiraError if anything
    required is missing."""
    missing = missing_settings()
    if missing:
        raise JiraError("Jira is not configured; missing: " + ", ".join(missing))
    return JiraClient(
        base_url=_setting("JIRA_BASE_URL"),
        project_key=_setting("JIRA_PROJECT_KEY"),
        token=_setting("JIRA_API_TOKEN"),
        email=_setting("JIRA_EMAIL"),
        **kwargs,
    )


# ------------------------------------------------------------
# Text helpers
# ------------------------------------------------------------

def _label(text: str) -> str:
    # Jira labels cannot contain spaces: lowercase, hyphen-separated.
    return re.sub(r"[^a-z0-9._-]+", "-", str(text).lower()).strip("-")


def _summary(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= SUMMARY_LIMIT else text[: SUMMARY_LIMIT - 1] + "…"


def _bullets(items: List[str]) -> str:
    return "\n".join(f"* {i}" for i in items)


def _fingerprint(epic: dict, stories: List[dict], project_key: str) -> str:
    blob = json.dumps({"p": project_key, "e": epic, "s": stories}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------
# The client
# ------------------------------------------------------------

class JiraClient:
    def __init__(
        self,
        base_url: str,
        project_key: str,
        token: str,
        email: str = "",
        session: Any = None,
        sleep=time.sleep,
    ):
        self.base_url = _normalise_url(base_url)
        self.project_key = project_key.strip()
        self._token = token
        self._email = email
        # `session` and `sleep` can be replaced in tests (no real network, no waiting).
        self._session = session or requests.Session()
        self._sleep = sleep
        self.dropped: List[dict] = []     # optional fields Jira would not accept
        self.warnings: List[str] = []
        # Discovered from the project (see _discover):
        self._types: Dict[str, str] = {}
        self._story_points_field = ""
        self._epic_name_field = ""
        self._epic_link_field = ""

    # ---------------- low-level HTTP ----------------

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def _send(self, method: str, path: str, body: Optional[dict] = None, allow=()) -> Any:
        """One API call, with retries for rate limits / server errors. Returns
        the response; non-success statuses raise JiraError unless listed in
        `allow` (the caller wants to inspect those, e.g. a 400)."""
        url = f"{self.base_url}{API}{path}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth = None
        if self._email:                                   # Jira Cloud: email + API token
            auth = (self._email, self._token)
        else:                                             # Data Center: personal access token
            headers["Authorization"] = f"Bearer {self._token}"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._session.request(
                    method, url, json=body, headers=headers, auth=auth, timeout=TIMEOUT_SECONDS
                )
            except requests.RequestException as exc:
                if attempt == MAX_ATTEMPTS:
                    raise JiraError(self._redact(f"Could not reach Jira at {self.base_url}: {exc}")) from None
                self._sleep(2 ** attempt)
                continue

            if resp.status_code in (429, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                try:
                    wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                except (TypeError, ValueError):
                    wait = 2 ** attempt
                self._sleep(min(wait, 30))
                continue

            if resp.status_code < 300 or resp.status_code in allow:
                return resp

            raise self._error_from(resp, f"{method} {path}")

    def _error_from(self, resp: Any, what: str) -> JiraError:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        errors = data.get("errors") or {}
        messages = list(data.get("errorMessages") or []) + [f"{k}: {v}" for k, v in errors.items()]
        hint = {
            401: "credentials were rejected - check JIRA_EMAIL and JIRA_API_TOKEN",
            403: "the account lacks permission for this action",
            404: "not found - check JIRA_BASE_URL and JIRA_PROJECT_KEY, and that the account can see the project",
        }.get(resp.status_code, "")
        detail = "; ".join(messages) or (resp.text or "")[:200]
        text = f"Jira {what} failed ({resp.status_code})" + (f" - {hint}" if hint else "") + (f": {detail}" if detail else "")
        return JiraError(self._redact(text), resp.status_code, errors)

    # ---------------- discovery ----------------

    def _discover(self) -> None:
        """Learn this project's issue types and the ids of the custom fields we
        use (story points, epic name / link), which differ between Jira sites."""
        # A common slip: pasting an ISSUE key (PROJ-12) instead of the PROJECT key (PROJ).
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-\d+", self.project_key):
            raise JiraError(
                f"JIRA_PROJECT_KEY is '{self.project_key}', which looks like an issue key. "
                f"Use the project key without the number, e.g. '{self.project_key.rsplit('-', 1)[0]}'."
            )
        try:
            project = self._send("GET", f"/project/{self.project_key}").json()
        except JiraError as exc:
            if exc.status == 404:
                # Distinguish "wrong key" from "this account can see no projects".
                visible = self._send("GET", "/project/search?maxResults=1", allow=(400, 403, 404))
                if visible.status_code == 200 and not visible.json().get("values"):
                    raise JiraError(
                        f"Project '{self.project_key}' was not found, and this account cannot see ANY "
                        f"project on {self.base_url}. Ask a Jira admin to give the account access to the "
                        f"project (Browse Projects and Create Issues permissions), or check the token "
                        f"belongs to the right account."
                    ) from None
            raise
        types = project.get("issueTypes") or []

        def pick(*names, subtask=None):
            for t in types:
                if subtask is not None and bool(t.get("subtask")) != subtask:
                    continue
                if t.get("name", "").lower() in names:
                    return t["id"]
            return ""

        epic = pick("epic", subtask=False)
        story = pick("story", subtask=False) or pick("task", subtask=False)
        sub = pick("sub-task", "subtask", "sub task", subtask=True) or next(
            (t["id"] for t in types if t.get("subtask")), ""
        )
        missing = [n for n, v in (("Epic", epic), ("Story/Task", story), ("Sub-task", sub)) if not v]
        if missing:
            raise JiraError(
                f"Project {self.project_key} has no usable issue type for: {', '.join(missing)}. "
                f"Enable them in the project's issue type scheme."
            )
        self._types = {"epic": epic, "story": story, "sub": sub}

        # Custom fields are found by their display name.
        for f in self._send("GET", "/field").json():
            name = (f.get("name") or "").lower()
            if name in ("story point estimate", "story points"):
                self._story_points_field = self._story_points_field or f["id"]
            elif name == "epic name":
                self._epic_name_field = f["id"]
            elif name == "epic link":
                self._epic_link_field = f["id"]

    # ---------------- creating one issue ----------------

    def _create(self, fields: dict, optional: set) -> str:
        """Create an issue and return its key. If Jira rejects an OPTIONAL
        field (not on the project's screen, unknown value...), drop that field,
        remember it, and try again; a rejected required field is an error."""
        for _ in range(5):
            resp = self._send("POST", "/issue", {"fields": fields}, allow=(400,))
            if resp.status_code < 300:
                return resp.json()["key"]
            err = self._error_from(resp, "create issue")
            bad = [k for k in err.errors if k in optional and k in fields]
            if not bad:
                raise err
            for k in bad:
                fields.pop(k)
                self.dropped.append({"field": k, "reason": str(err.errors[k])})
        raise err

    def _common(self, type_key: str, summary: str, description: str, labels: List[str]) -> dict:
        fields = {
            "project": {"key": self.project_key},
            "issuetype": {"id": self._types[type_key]},
            "summary": _summary(summary),
            "description": description,
        }
        if labels:
            fields["labels"] = labels
        return fields

    # ---------------- publishing a whole release ----------------

    def publish(
        self, epic: dict, stories: List[dict], run_id: str,
        state_path: Optional[Path] = None, selection: Optional[Iterable[str]] = None,
    ) -> dict:
        """Create the chosen items in Jira. `selection` is a set of keys (see
        plan_keys); None means everything. Items already created by an earlier
        attempt for this exact plan are never created twice."""
        all_keys = plan_keys({"stories": stories})
        wanted = set(all_keys) if selection is None else set(selection) & set(all_keys)
        if not wanted:
            raise JiraError("No tickets were selected, so nothing was sent to Jira.")

        fingerprint = _fingerprint(epic, stories, self.project_key)
        state = self._load_state(state_path, fingerprint)
        created: Dict[str, str] = state["created"]
        resumed = bool(created)

        # A task can only exist under its story: ignore (and say so) any task
        # whose story is neither selected nor already in Jira.
        sid_of = {f"{s['id']}/T{i}": s["id"] for s in stories for i, _ in enumerate(s["tasks"], start=1)}
        for tkey in sorted(k for k in wanted if k in sid_of):
            if sid_of[tkey] not in wanted and sid_of[tkey] not in created:
                wanted.discard(tkey)
                self.warnings.append(f"Sub-task {tkey} was skipped: its story {sid_of[tkey]} is not in Jira. Select the story too.")

        if not wanted:
            raise JiraError("; ".join(self.warnings) or "No tickets were selected, so nothing was sent to Jira.")

        todo = {k for k in wanted if k not in created}
        if not todo:                                   # everything chosen is already there
            return self._result(epic, stories, created, resumed=True)

        self._discover()
        run_label = _label(f"arch-run-{run_id}")
        footer = f"\n\n_Created by Claude SDLC Architect, run {run_id}._"

        def save():
            if state_path:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(
                    json.dumps({"fingerprint": fingerprint, "complete": set(all_keys) <= set(created),
                                "created": created}, indent=2),
                    encoding="utf-8",
                )

        try:
            # ---- Epic (only if chosen; without one, stories are created ungrouped) ----
            if "epic" in todo:
                fields = self._common("epic", epic["title"], epic["description"] + footer, [run_label])
                optional = {"labels"}
                if self._epic_name_field:
                    fields[self._epic_name_field] = _summary(epic["title"])
                    optional.add(self._epic_name_field)
                created["epic"] = self._create(fields, optional)
                save()
            epic_key = created.get("epic")
            if not epic_key and any(k in todo for k in sid_of.values()):
                self.warnings.append("No epic was created, so the new stories are not grouped under one.")

            # ---- Stories, each with its Sub-tasks (our Tasks) ----
            for story in stories:
                sid = story["id"]
                if sid in todo:
                    created[sid] = self._create_story(story, epic_key, run_label, footer)
                    save()
                story_key = created.get(sid)
                if not story_key:
                    continue

                for index, task in enumerate(story["tasks"], start=1):
                    tkey = f"{sid}/T{index}"
                    if tkey not in todo:
                        continue
                    steps = [s["title"] + (f": {s['description']}" if s.get("description") else "") for s in task["subtasks"]]
                    description = (task.get("description") or "") + (f"\n\nh3. Steps\n{_bullets(steps)}" if steps else "") + footer
                    fields = self._common("sub", task["title"], description.strip(), [run_label])
                    fields["parent"] = {"key": story_key}
                    created[tkey] = self._create(fields, {"labels"})
                    save()

            # ---- "Blocked by" links, between any two stories now in Jira ----
            for story in stories:
                for blocker in story.get("blocked_by", []):
                    lkey = f"link:{story['id']}<{blocker}"
                    if lkey in created:
                        continue
                    if story["id"] not in created or blocker not in created:
                        if story["id"] in todo:
                            self.warnings.append(
                                f"{story['id']} is blocked by {blocker}, which is not in Jira, so no link was made.")
                        continue
                    try:
                        self._send("POST", "/issueLink", {
                            "type": {"name": "Blocks"},
                            # outward issue "blocks" the inward issue
                            "inwardIssue": {"key": created[story["id"]]},
                            "outwardIssue": {"key": created[blocker]},
                        })
                        created[lkey] = "linked"
                        save()
                    except JiraError as exc:
                        self.warnings.append(f"Could not link {story['id']} as blocked by {blocker}: {exc}")
        except JiraError as exc:
            exc.created_count = sum(1 for k in created if not k.startswith("link:"))
            raise

        save()
        return self._result(epic, stories, created, resumed=resumed)

    def _create_story(self, story: dict, epic_key: Optional[str], run_label: str, footer: str) -> str:
        parts = [story.get("description") or ""]
        if story.get("acceptance_criteria"):
            parts.append("h3. Acceptance criteria\n" + _bullets(story["acceptance_criteria"]))
        meta = [f"Component: {story['component']}"] + ([f"Team: {story['team']}"] if story.get("team") else [])
        parts.append("h3. Details\n" + _bullets(meta))
        labels = list(dict.fromkeys(
            [l for l in (_label(x) for x in story.get("labels", [])) if l]
            + [_label(f"component-{story['component']}"), run_label]
        ))
        fields = self._common("story", story["title"], "\n\n".join(p for p in parts if p) + footer, labels)
        fields["priority"] = {"name": story["priority"]}
        optional = {"priority", "labels"}
        if self._story_points_field:
            fields[self._story_points_field] = float(story["story_points"])
            optional.add(self._story_points_field)

        # Newer projects place a story under its epic with `parent`; older
        # company-managed projects use the "Epic Link" custom field instead.
        if not epic_key:                       # no epic chosen: create the story ungrouped
            return self._create(fields, optional)
        fields["parent"] = {"key": epic_key}
        try:
            return self._create(dict(fields), optional)
        except JiraError as exc:
            if "parent" in exc.errors and self._epic_link_field:
                fields.pop("parent")
                fields[self._epic_link_field] = epic_key
                return self._create(fields, optional | {self._epic_link_field})
            raise

    # ---------------- state file + result ----------------

    def _load_state(self, state_path: Optional[Path], fingerprint: str) -> dict:
        """Progress from an earlier attempt, but only if it was for THIS exact
        plan. A different plan starts fresh (the old issues still exist in
        Jira, which we say so in a warning)."""
        if state_path and state_path.is_file():
            try:
                saved = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            if saved.get("fingerprint") == fingerprint:
                return {"created": dict(saved.get("created", {})), "complete": bool(saved.get("complete"))}
            old = [v for k, v in saved.get("created", {}).items() if not k.startswith("link:") and "/" not in k]
            if old:
                self.warnings.append(
                    "The plan changed since an earlier publish; issues from that attempt "
                    f"still exist in Jira ({', '.join(old)}) and were not touched."
                )
        return {"created": {}, "complete": False}

    def _result(self, epic: dict, stories: List[dict], created: Dict[str, str], resumed: bool) -> dict:
        """What exists in Jira for this plan, and what was left out. Only items
        that were actually created appear in `stories`."""
        url = lambda key: f"{self.base_url}/browse/{key}"
        story_out, not_sent = [], {"epic": "epic" not in created, "stories": [], "sub_tasks": []}
        for story in stories:
            tasks = []
            for index, task in enumerate(story["tasks"], start=1):
                key = created.get(f"{story['id']}/T{index}", "")
                if key:
                    tasks.append({"key": key, "url": url(key), "title": task["title"],
                                  "subtasks": [s["title"] for s in task["subtasks"]]})
                else:
                    not_sent["sub_tasks"].append({"story": story["title"], "title": task["title"]})
            key = created.get(story["id"], "")
            if key:
                story_out.append({"id": story["id"], "key": key, "url": url(key), "title": story["title"],
                                  "component": story["component"], "tasks": tasks})
            else:
                not_sent["stories"].append({"id": story["id"], "title": story["title"]})
        epic_key = created.get("epic")
        return {
            "status": "created",
            "base_url": self.base_url,
            "project_key": self.project_key,
            "epic": {"key": epic_key, "url": url(epic_key), "title": epic["title"]} if epic_key else None,
            "stories": story_out,
            "counts": {
                "epics": 1 if epic_key else 0,
                "stories": len(story_out),
                "sub_tasks": sum(len(s["tasks"]) for s in story_out),
            },
            "not_sent": not_sent,
            "dropped_fields": list(self.dropped),
            "warnings": list(self.warnings),
            "resumed": resumed,
            "mapping": MAPPING_NOTE,
        }
