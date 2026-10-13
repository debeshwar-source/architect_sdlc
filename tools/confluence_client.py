"""
Publishes the design document as a page in a REAL Confluence space, over Confluence's REST API.

Plain words: tools/design_doc.py builds the design document (text plus diagram images). This
module creates it as a Confluence page, attaches the diagram images to that page, and the
page then shows them. It only runs when the pipeline calls it after a person has said yes
(see confluence_publish_node in orchestrator/graph.py); nothing here talks to the network
until publish() is called.

Settings come from the environment / the git-ignored .env file. Confluence Cloud uses the
SAME Atlassian site, account and API token as Jira, so those are reused:

    JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN     (shared with Jira)
    CONFLUENCE_SPACE_KEY       the space to publish into (the short code in the space's address)
    CONFLUENCE_PARENT_PAGE_ID  optional: put the page under this existing page
    CONFLUENCE_BASE_URL        optional: only if Confluence is at a different address than Jira
    CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN   optional: only if it needs different credentials

The account needs permission to add pages and attachments in that space.

Safety properties
-----------------
* Never overwrites someone else's page: if a page with the title already exists in the space
  (and this run did not create it), publishing stops and says so.
* Idempotent and resumable: progress is saved in runs/<run_id>/confluence_created.json.
  Publishing the same document again does nothing; after a failure it resumes where it
  stopped; if the wording was edited since, the page this run created is UPDATED in place
  (a new version), never duplicated.
* The API token is never logged and is scrubbed from any error text.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 4          # per request, for rate limits (429) and server hiccups (5xx)


class ConfluenceError(Exception):
    """Anything that goes wrong talking to Confluence."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------
# Settings and the no-network helpers the UI / CLI use
# ------------------------------------------------------------

def _setting(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _first(*names: str) -> str:
    return next((v for v in (_setting(n) for n in names) if v), "")


def _normalise_base(url: str) -> str:
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url):
        url = f"https://{url}"
    # Confluence Cloud lives under /wiki on the Atlassian site.
    if re.match(r"^https://[^/]+\.atlassian\.net$", url):
        url += "/wiki"
    return url


def missing_settings() -> List[str]:
    """Names of settings still needed. (The email is only needed on Confluence Cloud.)"""
    missing = []
    if not _first("CONFLUENCE_BASE_URL", "JIRA_BASE_URL"):
        missing.append("JIRA_BASE_URL (or CONFLUENCE_BASE_URL)")
    if not _first("CONFLUENCE_API_TOKEN", "JIRA_API_TOKEN"):
        missing.append("JIRA_API_TOKEN (or CONFLUENCE_API_TOKEN)")
    if not _setting("CONFLUENCE_SPACE_KEY"):
        missing.append("CONFLUENCE_SPACE_KEY")
    return missing


def is_configured() -> bool:
    return not missing_settings()


def preview(title: str = "", attachments: int = 0) -> Dict[str, Any]:
    """What publishing WOULD do, for the permission prompt. No network calls, no secrets."""
    base = _first("CONFLUENCE_BASE_URL", "JIRA_BASE_URL")
    return {
        "configured": is_configured(),
        "missing": missing_settings(),
        "base_url": _normalise_base(base) if base else "",
        "space_key": _setting("CONFLUENCE_SPACE_KEY"),
        "parent_page_id": _setting("CONFLUENCE_PARENT_PAGE_ID"),
        "account": _first("CONFLUENCE_EMAIL", "JIRA_EMAIL") or "(token only)",
        "title": title,
        "attachments": attachments,
    }


def from_env(**kwargs) -> "ConfluenceClient":
    missing = missing_settings()
    if missing:
        raise ConfluenceError("Confluence is not configured; missing: " + ", ".join(missing))
    return ConfluenceClient(
        base_url=_first("CONFLUENCE_BASE_URL", "JIRA_BASE_URL"),
        space_key=_setting("CONFLUENCE_SPACE_KEY"),
        token=_first("CONFLUENCE_API_TOKEN", "JIRA_API_TOKEN"),
        email=_first("CONFLUENCE_EMAIL", "JIRA_EMAIL"),
        parent_id=_setting("CONFLUENCE_PARENT_PAGE_ID"),
        **kwargs,
    )


# ------------------------------------------------------------
# The client
# ------------------------------------------------------------

class ConfluenceClient:
    def __init__(self, base_url: str, space_key: str, token: str, email: str = "", parent_id: str = "",
                 session: Any = None, sleep=time.sleep):
        self.base_url = _normalise_base(base_url)
        self.api = f"{self.base_url}/rest/api"
        self.space_key = space_key.strip()
        self.parent_id = parent_id.strip()
        self._token = token
        self._email = email
        self._session = session or requests.Session()      # replaceable in tests (no real network)
        self._sleep = sleep
        self.warnings: List[str] = []

    # ---------------- low-level HTTP ----------------

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def _send(self, method: str, path: str, body: Optional[dict] = None, files: Optional[dict] = None,
              data: Optional[dict] = None, params: Optional[dict] = None, allow=()) -> Any:
        """One API call, with retries for rate limits / server errors. Non-success statuses
        raise ConfluenceError unless listed in `allow`."""
        url = f"{self.api}{path}"
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if files is not None:
            headers["X-Atlassian-Token"] = "no-check"        # Confluence requires this for uploads
        auth = None
        if self._email:                                      # Confluence Cloud: email + API token
            auth = (self._email, self._token)
        else:                                                # Data Center: personal access token
            headers["Authorization"] = f"Bearer {self._token}"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._session.request(method, url, json=body, files=files, data=data, params=params,
                                             headers=headers, auth=auth, timeout=TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                if attempt == MAX_ATTEMPTS:
                    raise ConfluenceError(self._redact(f"Could not reach Confluence at {self.base_url}: {exc}")) from None
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

    def _error_from(self, resp: Any, what: str) -> ConfluenceError:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        detail = data.get("message") or "; ".join(map(str, data.get("errorMessages") or [])) or (resp.text or "")[:200]
        hint = {
            401: "credentials were rejected - check the email and API token",
            403: f"the account lacks permission here (it needs to add pages and attachments in space {self.space_key})",
            404: "not found - check the address, the space key and the parent page ID",
        }.get(resp.status_code, "")
        text = f"Confluence {what} failed ({resp.status_code})" + (f" - {hint}" if hint else "") + (f": {detail}" if detail else "")
        return ConfluenceError(self._redact(text), resp.status_code)

    # ---------------- helpers ----------------

    def _check_space(self) -> None:
        resp = self._send("GET", f"/space/{self.space_key}", allow=(404,))
        if resp.status_code == 404:
            visible = self._send("GET", "/space", params={"limit": 1}, allow=(400, 403, 404))
            if visible.status_code == 200 and not (visible.json().get("results") or []):
                raise ConfluenceError(
                    f"Space '{self.space_key}' was not found, and this account cannot see ANY space on "
                    f"{self.base_url}. Ask a Confluence admin to give the account access to the space, or "
                    f"check the token belongs to the right account.")
            raise ConfluenceError(
                f"Space '{self.space_key}' was not found, or this account cannot see it. Check "
                f"CONFLUENCE_SPACE_KEY (it is the short code in the space's address).", 404)

    def _page_url(self, page: dict, page_id: str) -> str:
        links = page.get("_links") or {}
        if links.get("webui"):
            return (links.get("base") or self.base_url) + links["webui"]
        return f"{self.base_url}/spaces/{self.space_key}/pages/{page_id}"

    def _upload(self, page_id: str, name: str, content: bytes) -> None:
        """Attach a file to the page, or replace the file of that name if it is already attached."""
        existing = self._send("GET", f"/content/{page_id}/child/attachment", params={"filename": name}).json()
        found = (existing.get("results") or [])
        files = {"file": (name, content, "image/png")}
        if found:
            self._send("POST", f"/content/{page_id}/child/attachment/{found[0]['id']}/data",
                       files=files, data={"minorEdit": "true"})
        else:
            self._send("POST", f"/content/{page_id}/child/attachment", files=files, data={"minorEdit": "true"})

    # ---------------- publishing ----------------

    def publish(self, title: str, body: str, attachments: Dict[str, bytes], run_id: str,
                state_path: Optional[Path] = None) -> dict:
        """Create the page (or update the one this run already created) and attach the images."""
        title = " ".join(title.split())
        if not title:
            raise ConfluenceError("The page needs a title.")
        fingerprint = hashlib.sha256(json.dumps(
            {"s": self.space_key, "t": title, "b": body,
             "a": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(attachments.items())}}, sort_keys=True,
        ).encode("utf-8")).hexdigest()
        state = self._load_state(state_path)

        if state.get("complete") and state.get("fingerprint") == fingerprint:
            return self._result(title, state, attachments, updated=False, unchanged=True)

        self._check_space()
        page_id = state.get("page_id")
        page: dict = {"_links": {"webui": state.get("webui")}} if state.get("webui") else {}

        def save(**fields):
            state.update(fields)
            if state_path:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        if not page_id:
            # Never take over a page somebody else wrote.
            clash = self._send("GET", "/content", params={"spaceKey": self.space_key, "title": title, "type": "page", "limit": 1}).json()
            if clash.get("results"):
                raise ConfluenceError(f"A page called '{title}' already exists in space {self.space_key}. "
                                      f"Choose a different title (edit the document title) and publish again.", 400)
            payload = {"type": "page", "title": title, "space": {"key": self.space_key},
                       "body": {"storage": {"value": body, "representation": "storage"}}}
            if self.parent_id:
                payload["ancestors"] = [{"id": self.parent_id}]
            page = self._send("POST", "/content", body=payload).json()
            page_id = str(page["id"])
            save(page_id=page_id, webui=(page.get("_links") or {}).get("webui"), attachments_done=[],
                 complete=False, fingerprint="", synced_fingerprint=fingerprint, title=title)

        # Is the content on the page out of date? (An earlier attempt may have stopped part-way
        # with the SAME content: then we only send what is missing. If the wording was edited
        # since, the page is updated in place and the images are sent again.)
        updated = state.get("synced_fingerprint") != fingerprint
        done = set() if updated else set(state.get("attachments_done") or [])
        for name, content in attachments.items():
            if name in done:
                continue
            self._upload(page_id, name, content)
            done.add(name)
            save(attachments_done=sorted(done))

        if updated:
            current = self._send("GET", f"/content/{page_id}", params={"expand": "version"}).json()
            page = self._send("PUT", f"/content/{page_id}", body={
                "id": page_id, "type": "page", "title": title,
                "version": {"number": int((current.get("version") or {}).get("number", 1)) + 1},
                "body": {"storage": {"value": body, "representation": "storage"}},
            }).json()
            save(webui=(page.get("_links") or {}).get("webui") or state.get("webui"), title=title,
                 synced_fingerprint=fingerprint)

        save(complete=True, fingerprint=fingerprint, synced_fingerprint=fingerprint, title=title)
        return self._result(title, state, attachments, updated=updated, unchanged=False)

    def _load_state(self, state_path: Optional[Path]) -> dict:
        if state_path and state_path.is_file():
            try:
                return json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return {}

    def _result(self, title: str, state: dict, attachments: Dict[str, bytes], updated: bool, unchanged: bool) -> dict:
        page_id = state["page_id"]
        url = self._page_url({"_links": {"webui": state.get("webui")}} if state.get("webui") else {}, page_id)
        return {
            "status": "published", "title": title, "page_id": page_id, "url": url,
            "space_key": self.space_key, "base_url": self.base_url,
            "attachments": sorted(attachments), "updated": updated, "unchanged": unchanged,
            "warnings": list(self.warnings),
        }
