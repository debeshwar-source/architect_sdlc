"""Design Agent -- the core planning role (formerly the "Architecture Agent").

Plain words: the second stage and the "brain" of the plan. It reads the
requirement and decides how to build it:
  - Is any change needed at all?
  - Which existing applications must change, and in what order?
  - Are brand-new applications required?
  - Exactly WHAT has to be made (every service, UI, database, queue, job and outside
    system involved) and how all of it is connected: the architecture itself.
  - How will it be tested, and what could go wrong?

The knowledge base (descriptions of the existing applications) is OPTIONAL.
  - With a knowledge base, the agent grounds its plan in those documents.
  - With none (an empty or missing folder), it designs from scratch: every
    application it needs is proposed as a NEW application.
Either way it can return `knowledge_base_updates`: new documents to create,
or additions to append to existing ones, so the knowledge base grows as the
system is designed. (The pipeline applies them at the final step - see
orchestrator/graph.py - this agent only proposes them.)
"""

from typing import Any, Dict, List, Optional

import llm
import token_log
from agents._common import (
    format_dict,
    format_feedback_section,
    load_skill,
    validate_clarifying_questions,
)
from knowledge_base.loader import (
    SCRATCH_KB_NOTICE,
    ApplicationDoc,
    application_names,
    canonical_name,
    kb_page,
)

# Added to the one-page knowledge base summary in one-pager mode.
READ_RULES = """

HOW TO USE THIS: the knowledge base above is a one-page summary and an index; the full documents are NOT
included. Before you name an application as affected, or rely on its contract or data, read the relevant
section with the `read_kb` tool (document name, section name from the index). Read only what the summary
does not settle, at most {n} reads, and prefer sections to whole documents."""

# Fields the AI's answer must contain, or the answer is rejected.
REQUIRED_KEYS = [
    "change_required",
    "affected_applications",
    "unaffected_applications_considered",
    "new_applications_needed",
    "cross_service_contracts",
    "dependency_order",
    "overview",
    "components",
    "connections",
    "flows",
    "test_strategy",
    "architecture_risks",
    "knowledge_base_updates",
    "clarifying_questions",
]

# What a "component" (a thing that is built, changed, used or depended on) can be, and
# what state it is in. These drive how it is drawn and how it is checked.
COMPONENT_KINDS = {"service", "client", "gateway", "database", "messaging", "cache", "job", "library", "external"}
COMPONENT_STATUSES = {"new", "changed", "existing", "external"}
# Components of these kinds are APPLICATIONS (they get a knowledge-base document and
# Jira stories); the others (a database, a queue...) belong to an application.
APPLICATION_KINDS = {"service", "client", "gateway"}
CONNECTION_KINDS = {"sync_api", "async_event", "data_access", "batch", "ui_call"}
FLOW_STEP_KINDS = {"call", "async", "return"}

# The only two things a knowledge-base update may do.
VALID_KB_ACTIONS = {"create", "augment"}


def run(
    requirement: Dict[str, Any],
    applications: List[ApplicationDoc],
    revision_feedback: Dict[str, Any] = None,
    model: Optional[str] = None,
    context_notes: str = "",
    from_scratch: bool = False,
) -> Dict[str, Any]:
    # FROM-SCRATCH BUILD: the existing knowledge base must play no part. It
    # is removed here, in the agent itself, so this holds no matter what the
    # caller passes in: the AI is never shown it, and the validation below
    # (which checks names against `applications`) then rejects any answer
    # that mentions an existing application.
    if from_scratch:
        applications = []

    # Reviewer feedback / background notes (empty on a first attempt).
    feedback_section = format_feedback_section(revision_feedback, context_notes)

    # What of the knowledge base to show: the documents relevant to this request, and inside the
    # big ones only the relevant sections (plus the contracts/limits sections and the foundational
    # documents). Everything else is still listed - as a heading or a one-line summary - so the AI
    # can name it. A small knowledge base, or CLAUDE_SDLC_ARCHITECT_KB_SLICING=0, shows everything.
    # The knowledge base is one page (knowledge_base/<name>.md), shown as it is. It comes FIRST and the
    # requirement after it: the page is the same from run to run, so the AI service can reuse it from its
    # cache; only what follows the first difference has to be paid for again.
    kb_text = SCRATCH_KB_NOTICE if from_scratch else kb_page(applications)
    # The checker compares names exactly, and an application is named after its file (e.g. "d2c_user"),
    # not after the page's heading ("D2C-User-Data"), so the exact names are spelled out.
    names_line = ("EXACT NAMES of existing applications (use these spellings only): "
                  + ", ".join(application_names(applications))
                  + ". Any other application must go in new_applications_needed.\n\n") if applications else ""
    prompt = f"""
============================================================
KNOWLEDGE BASE (every known application)
============================================================

{names_line}{kb_text}

============================================================
REQUIREMENT
============================================================

{format_dict(requirement)}
{feedback_section}
============================================================
OUTPUT
============================================================

Return ONLY valid JSON matching the contract in your skill.
"""
    token_log.record("kb_page", "Design Agent", applications=len(applications), kb_tokens=token_log.est_tokens(kb_text))

    # Send the prompt to the AI. `required_keys` lists the fields the answer must contain; `system` is this
    # agent's skill (skills/design.md); `model` optionally picks a specific AI model.
    result = llm.call_llm_json(
        prompt,
        "Design Agent",
        required_keys=REQUIRED_KEYS,
        system=load_skill("design"),
        model=model,
        validate=lambda r: (_use_known_spellings(r, application_names(applications)),
                            _validate(r, application_names(applications))),   # bad answer -> corrected, not fatal
    )

    # Use the knowledge base's own spelling for any application the AI wrote differently ("d2c-user" for
    # "d2c_user"), then double-check the answer makes logical sense (see _validate).
    _use_known_spellings(result, application_names(applications))
    _validate(result, application_names(applications))
    _clean_architecture(result)
    _clean_kb_updates(result)
    validate_clarifying_questions(result, "Design Agent")
    return result


_NAME_KEYS = {"name", "application", "from", "to"}


def _use_known_spellings(result: Any, known_apps: List[str]) -> None:
    """Rewrite, in place, every application name in the answer that matches a known application apart from
    spelling (case, '_' vs '-') to the known spelling, so the exact-name checks below do not reject it. Only
    whole values under name-like keys (and the items of dependency_order) are touched, never prose."""
    def fix(value: Any) -> Any:
        return (canonical_name(value, known_apps) or value) if isinstance(value, str) else value

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _NAME_KEYS:
                    node[key] = fix(value)
                elif key == "dependency_order" and isinstance(value, list):
                    node[key] = [fix(v) for v in value]
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(result)


def _validate(result: Dict[str, Any], known_apps: List[str]) -> None:
    """Reject self-contradicting answers (raises ValueError, which makes
    the AI call be retried)."""
    # "change_required" must be strictly true/false.
    if not isinstance(result["change_required"], bool):
        raise ValueError("Design Agent 'change_required' must be a boolean.")

    # Collect the names of applications to change and of new ones to build.
    affected_names = {a["name"] for a in result["affected_applications"]}
    new_names = {a["name"] for a in result["new_applications_needed"]}

    # Every application named must either exist already or be explicitly
    # proposed as new - no invented applications. (With an empty knowledge
    # base nothing "exists", so everything must be proposed as new.)
    unknown = affected_names - set(known_apps) - new_names
    if unknown:
        raise ValueError(
            f"Design Agent referenced unknown application(s) not in "
            f"the knowledge base and not proposed as new: {unknown}"
        )

    # Says "change needed" but names nothing to change -> contradiction.
    if result["change_required"] and not affected_names and not new_names:
        raise ValueError(
            "Design Agent requires a change but named no affected "
            "or new applications."
        )

    # Says "no change needed" but lists things to change -> contradiction.
    if not result["change_required"] and (affected_names or new_names):
        raise ValueError(
            "Design Agent named applications to change despite "
            "change_required being false."
        )

    # The build order may only mention applications that are part of the plan.
    dependency_order = result["dependency_order"]
    all_names = affected_names | new_names
    order_names = set(dependency_order)
    if order_names - all_names:
        raise ValueError(
            f"dependency_order references application(s) not in "
            f"affected_applications/new_applications_needed: "
            f"{order_names - all_names}"
        )

    if result["change_required"]:
        _validate_architecture(result, known_apps, affected_names, new_names)


def _text(value: Any) -> str:
    return " ".join(str(value if value is not None else "").split())


def _validate_architecture(result: Dict[str, Any], known_apps: List[str],
                           affected_names: set, new_names: set) -> None:
    """The components / connections / flows must be complete and consistent with the rest
    of the plan, so the diagrams and the document built from them can be trusted. Every
    message here is shown to the AI if it has to correct itself, so each says what to fix."""
    if not _text(result.get("overview")):
        raise ValueError("overview is empty: write a short plain-language description of the architecture.")

    components = result.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("components is empty: list every service, UI, database, queue, job and outside "
                         "system that is built, changed or used.")
    by_name: Dict[str, Dict[str, Any]] = {}
    for c in components:
        if not isinstance(c, dict) or not _text(c.get("name")):
            raise ValueError("every component needs a name.")
        name = _text(c["name"])
        if name in by_name:
            raise ValueError(f"component {name!r} is listed twice.")
        if c.get("kind") not in COMPONENT_KINDS:
            raise ValueError(f"component {name!r} has kind {c.get('kind')!r}; it must be one of {sorted(COMPONENT_KINDS)}.")
        if c.get("status") not in COMPONENT_STATUSES:
            raise ValueError(f"component {name!r} has status {c.get('status')!r}; it must be one of {sorted(COMPONENT_STATUSES)}.")
        if not _text(c.get("responsibility")):
            raise ValueError(f"component {name!r} has no responsibility: say what it is for.")
        by_name[name] = c

    for name, c in by_name.items():
        app_like = c["kind"] in APPLICATION_KINDS
        if c["status"] == "changed" and name not in affected_names:
            raise ValueError(f"component {name!r} is marked changed but is not in affected_applications.")
        if c["status"] == "new" and app_like and name not in new_names:
            raise ValueError(f"component {name!r} is a new application but is not in new_applications_needed.")
        if c["status"] == "existing" and app_like and name not in known_apps:
            raise ValueError(f"component {name!r} is marked existing but is not a known application "
                             f"(mark it external, or new if it has to be built).")
    for name in sorted(affected_names):
        if name not in by_name or by_name[name]["status"] != "changed":
            raise ValueError(f"affected application {name!r} must appear in components with status changed.")
    for name in sorted(new_names):
        if name not in by_name or by_name[name]["status"] != "new":
            raise ValueError(f"new application {name!r} must appear in components with status new.")

    connections = result.get("connections")
    if not isinstance(connections, list):
        raise ValueError("connections must be a list.")
    linked = set()
    for conn in connections:
        if not isinstance(conn, dict):
            raise ValueError("every connection must be an object.")
        a, b = _text(conn.get("from")), _text(conn.get("to"))
        if a not in by_name or b not in by_name:
            raise ValueError(f"connection {a!r} -> {b!r} names a component that is not in components.")
        if a == b:
            raise ValueError(f"connection {a!r} -> {b!r} joins a component to itself.")
        if not _text(conn.get("label")):
            raise ValueError(f"connection {a!r} -> {b!r} needs a label saying what passes along it.")
        if conn.get("kind") not in (None, "") and conn["kind"] not in CONNECTION_KINDS:
            raise ValueError(f"connection {a!r} -> {b!r} has kind {conn['kind']!r}; it must be one of {sorted(CONNECTION_KINDS)}.")
        linked.update((a, b))
    if len(by_name) >= 2 and not connections:
        raise ValueError("components are listed but connections is empty: show how they are connected.")
    for name, c in by_name.items():
        if len(by_name) >= 2 and name not in linked and c["kind"] != "library":
            raise ValueError(f"component {name!r} has no connection to anything; connect it, or leave it out.")

    flows = result.get("flows")
    if not isinstance(flows, list):
        raise ValueError("flows must be a list (it may be empty).")
    for flow in flows:
        if not isinstance(flow, dict) or not _text(flow.get("name")):
            raise ValueError("every flow needs a name.")
        steps = flow.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"flow {flow.get('name')!r} has no steps.")
        for step in steps:
            if not isinstance(step, dict) or _text(step.get("from")) not in by_name or _text(step.get("to")) not in by_name:
                raise ValueError(f"flow {flow['name']!r} has a step that names a component not in components.")
            if not _text(step.get("message")):
                raise ValueError(f"flow {flow['name']!r} has a step with no message.")
            if step.get("kind") not in (None, "") and step["kind"] not in FLOW_STEP_KINDS:
                raise ValueError(f"flow {flow['name']!r} has a step of kind {step['kind']!r}; use one of {sorted(FLOW_STEP_KINDS)}.")


def _clean_architecture(result: Dict[str, Any]) -> None:
    """Tidy the architecture in place (the checks above have passed): trim text, fill in
    optional fields, and give each connection a status. With no change required there is
    nothing to draw, so the lists are emptied."""
    result["overview"] = _text(result.get("overview"))
    if not result.get("change_required"):
        result["components"], result["connections"], result["flows"] = [], [], []
        return

    def lines(value) -> List[str]:
        items = value if isinstance(value, list) else ([value] if value else [])
        return [t for t in (_text(i) for i in items) if t]

    components = []
    for c in result["components"]:
        components.append({
            "name": _text(c["name"]), "kind": c["kind"], "status": c["status"],
            "responsibility": _text(c["responsibility"]), "technology": _text(c.get("technology")),
            "interfaces": lines(c.get("interfaces")), "data_owned": lines(c.get("data_owned")),
        })
    status_of = {c["name"]: c["status"] for c in components}
    connections = []
    for conn in result["connections"]:
        a, b = _text(conn["from"]), _text(conn["to"])
        default = ("new" if "new" in (status_of[a], status_of[b])
                   else "changed" if "changed" in (status_of[a], status_of[b]) else "existing")
        connections.append({
            "from": a, "to": b, "kind": conn.get("kind") or "sync_api",
            "protocol": _text(conn.get("protocol")), "label": _text(conn["label"]),
            "description": _text(conn.get("description")), "contract": _text(conn.get("contract")),
            "status": conn.get("status") if conn.get("status") in ("new", "changed", "existing") else default,
        })
    flows = []
    for flow in result["flows"]:
        flows.append({
            "name": _text(flow["name"]), "description": _text(flow.get("description")),
            "steps": [{"from": _text(s["from"]), "to": _text(s["to"]), "message": _text(s["message"]),
                       "kind": s.get("kind") or "call"} for s in flow["steps"]],
        })
    result["components"], result["connections"], result["flows"] = components, connections, flows


def _clean_kb_updates(result: Dict[str, Any]) -> None:
    """Tidy the proposed knowledge-base updates, in place. Like the
    clarifying questions, this is a best-effort extra: a malformed entry
    is dropped rather than failing the whole run over it."""
    updates = result.get("knowledge_base_updates")
    # No change needed -> nothing worth recording in the knowledge base.
    if not result.get("change_required") or not isinstance(updates, list):
        result["knowledge_base_updates"] = []
        return

    cleaned = []
    for u in updates:
        # Keep only well-formed entries: a name, a known action, and
        # actual content to write.
        if (
            isinstance(u, dict)
            and str(u.get("application") or "").strip()
            and u.get("action") in VALID_KB_ACTIONS
            and str(u.get("content") or "").strip()
        ):
            cleaned.append(
                {
                    "application": str(u["application"]).strip(),
                    "action": u["action"],
                    "content": str(u["content"]),
                    "reason": str(u.get("reason") or ""),
                }
            )
    result["knowledge_base_updates"] = cleaned
