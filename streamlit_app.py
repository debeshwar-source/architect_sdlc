"""Blueprint: the web page (UI) for the SDLC architect.

Plain words: this file draws the website you use in the browser. It shows a
form where you type your requirement, then runs the same pipeline as the
command line (cli.py) but displays each stage's result as it finishes.
In "Human approval" mode it pauses after every stage so a person can
approve, answer the AI's questions, give suggestions, or stop the run.

Streamlit note: Streamlit re-runs this whole file from top to bottom every
time the user clicks something. Values that must survive between those
re-runs are kept in `st.session_state` (a per-visitor memory).
"""

import copy
import itertools
import json
import os
from pathlib import Path
import random
import re
import threading
import time

import streamlit as st

import agents.discuss as discuss_agent
import agents.quality as quality_agent
import config
import knowledge_base.loader as kb_loader
import orchestrator.graph as graph
import run_log
import session_store
import tools.confluence_client as confluence_client
import tools.design_doc as design_doc
import tools.jira_client as jira_client
import tools.requirement_doc as requirement_doc
import tools.test_plan_doc as test_plan_doc
from hive import blackboard, events

# Browser-tab title, icon and use the full page width.
# Unique ids for the download buttons. (This line runs afresh on every Streamlit
# re-run, so the numbering restarts each time and stays the same from run to run.)
_DOWNLOAD_KEYS = itertools.count(1)


def _save_to_downloads(key: str, data: bytes, file_name: str) -> None:
    # Desktop window: write the file straight into the Downloads folder (never overwriting) and
    # remember where, so the page can say so. Nothing is opened, so the person stays on the page.
    try:
        folder = Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        target, n = folder / file_name, 1
        while target.exists():
            target = folder / f"{Path(file_name).stem} ({n}){Path(file_name).suffix}"
            n += 1
        target.write_bytes(data)
        st.session_state["_download_notice"] = (key, f"Saved to {target}")
    except OSError as exc:
        st.session_state["_download_notice"] = (key, f"Could not save the file: {exc}")


def offer_download(column, label: str, data: bytes, file_name: str, mime: str, key: str) -> None:
    """A download button. In a browser it is Streamlit's normal download. In the desktop window
    (where a download link would navigate the window to the file, with no way back) it saves the
    file to the Downloads folder in place and says where."""
    if not os.environ.get("BLUEPRINT_PARENT_PID"):
        column.download_button(label, data, file_name=file_name, mime=mime, key=key,
                               on_click="ignore", use_container_width=True)
        return
    column.button(label, key=key, use_container_width=True,
                  on_click=_save_to_downloads, args=(key, data, file_name))
    notice = st.session_state.get("_download_notice")
    if notice and notice[0] == key:
        column.caption(notice[1])

st.set_page_config(page_title="Blueprint · Claude SDLC", page_icon="📐", layout="wide")

# A little custom styling (CSS): smaller code text, a small uppercase label
# above the title, and a bolder title.
st.markdown(
    """
    <style>
    div[data-testid="stCodeBlock"] pre {
        font-size: 0.82rem;
    }
    .app-eyebrow {
        text-transform: uppercase;
        letter-spacing: 0.14em;
        font-size: 0.72rem;
        font-weight: 600;
        color: #6B7280;
        margin-bottom: -0.6rem;
    }
    h1 {
        font-weight: 700 !important;
        letter-spacing: -0.02em;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---- Page header ----
st.markdown('<p class="app-eyebrow">Claude SDLC</p>', unsafe_allow_html=True)
st.title("Blueprint")
st.markdown(
    "Turn one requirement into a design and a ready-to-file Jira backlog — "
    "epic, stories, tasks and subtasks — reasoned end-to-end by Claude. "
    "Works with or without an existing knowledge base."
)

# The knowledge bases that exist (each folder under knowledge_base/ is one).
# They are optional: the user can also choose "create something new".
knowledge_bases = kb_loader.list_knowledge_bases()

# The option in the knowledge-base picker that means "start something new".
NEW_KB_LABEL = "✚ Create something new (a new knowledge base)"

if knowledge_bases:
    # A collapsible panel to read what each knowledge base contains (one tab each).
    with st.expander(f"Knowledge bases ({len(knowledge_bases)})", expanded=False):
        st.caption(
            "Pick one when you start a run, or create something new. Nothing is "
            "added to a knowledge base without your say-so at the end of the release."
        )
        for tab, kb in zip(st.tabs([k.name for k in knowledge_bases]), knowledge_bases):
            with tab:
                docs = kb_loader.load_all(kb.path)
                if not docs:
                    st.caption("This knowledge base is empty.")
                for app in docs:
                    st.markdown(f"#### {app.name}")
                    st.markdown(app.content)
                    st.divider()
else:
    st.info(
        "No knowledge bases yet — your run will design from scratch. At the end of the "
        "release you will be asked whether to save what it designed as a new knowledge base."
    )


# Display label per agent key (config.AGENT_NAMES) -- used by the
# form's per-agent model pickers below.
AGENT_DISPLAY_NAMES = {
    "requirement": "Requirement",
    "design": "Design",
    "quality": "Quality Planner",
    "release": "Engineering Manager",
}

# The "Start with" choices -> the stage they mean.
START_OPTIONS = {"Requirement agent": "requirement", "Design agent (skip the Requirement agent)": "design"}

# ============================================================
# INPUT FORM
# ============================================================

st.subheader("Start a new run")

# The form groups the inputs so nothing happens until "Draft the blueprint"
# is clicked.
with st.container(border=True):
    with st.form("run_form"):
        # Big text box for the user's request.
        requirement_text = st.text_area(
            "Requirement",
            height=140,
            help="Plain language is fine -- naming specific applications is the Design stage's job, not yours. Works for changes to existing systems and for building something from scratch.",
            placeholder=(
                "Notify users by email and push when a new episode of a show "
                "they're watching becomes available, respecting their existing "
                "notification preferences."
            ),
        )

        # Choose between unattended ("Agentic") and paused-at-each-stage mode.
        mode = st.radio(
            "Mode",
            ["Agentic", "Human approval"],
            horizontal=True,
            captions=[
                "Runs start to finish, unattended.",
                "Pauses after each stage for your review and input.",
            ],
            help=(
                "Human approval pauses after Requirement, Design and Release "
                "Planning -- at each "
                "pause, the agent's own clarifying questions (with concrete "
                "options) are put to you directly, alongside open space for "
                "your own suggestions. Your answers go back to that same "
                "stage, which revises its own output before you're asked "
                "again."
            ),
        )
        human_in_the_loop = mode == "Human approval"

        # Which knowledge base to design against: one of the existing ones, or
        # "create something new". (One picker rather than a toggle plus a list,
        # because widgets inside a form cannot show or hide each other.)
        kb_options = [f"{k.name} ({k.application_count} applications)" for k in knowledge_bases] + [NEW_KB_LABEL]
        kb_choice = st.selectbox(
            "Knowledge base",
            kb_options,
            index=0,
            help=(
                "Choose an existing knowledge base for the agents to design against, or "
                "create something new. 'Create something new' hides every existing knowledge "
                "base and past run from the agents, so the design cannot mention or integrate "
                "with an existing system; at the end you are asked whether to save what it "
                "designed as a new knowledge base. Even if you pick an existing one, a request "
                "that clearly asks for a from-scratch build is treated as new."
            ),
        )
        # "" means create something new; otherwise the chosen knowledge base's name.
        selected_kb = "" if kb_choice == NEW_KB_LABEL else knowledge_bases[kb_options.index(kb_choice)].name

        # Where the run starts: with the Requirement agent (normal), or skip it and let the
        # Design agent work from your text exactly as written.
        start_choice = st.selectbox(
            "Start with", list(START_OPTIONS), index=0,
            help=(
                "The Requirement agent turns your request into a structured requirement. Choose the "
                "Design agent to skip it: your text is then used as the requirement exactly as written "
                "(useful when it is already precise)."
            ),
        )

        # Optional: pick a different AI model for each agent. Only choices
        # that differ from the default are remembered.
        agent_model_choices = {}
        with st.expander("🧠 Model per agent", expanded=False):
            st.caption(f"Default for every agent: `{config.MODEL_ID}`")
            for agent_key in config.AGENT_NAMES:
                chosen = st.selectbox(
                    AGENT_DISPLAY_NAMES.get(agent_key, agent_key),
                    config.AVAILABLE_MODELS,
                    index=(
                        config.AVAILABLE_MODELS.index(config.MODEL_ID)
                        if config.MODEL_ID in config.AVAILABLE_MODELS
                        else 0
                    ),
                    key=f"model_{agent_key}",
                )
                if chosen != config.MODEL_ID:
                    agent_model_choices[agent_key] = chosen

        # The submit button; `submitted` is True on the run where it was clicked.
        submitted = st.form_submit_button(
            "Draft the blueprint", type="primary", use_container_width=True
        )


# ============================================================
# STAGE METADATA
# ============================================================

# Pipeline-bar entries: (internal name, label shown, icon).
STAGE_META = [
    ("requirement", "Requirement", ":material/description:"),
    ("design", "Design", ":material/architecture:"),
    ("quality_planning", "Quality", ":material/fact_check:"),
    ("release_apply", "Engineering", ":material/engineering:"),
]

# Steps that share a slot on the progress bar with another step.
STAGE_ALIASES = {
    "release_planning": "release_apply",
    "jira_publish": "release_apply",
    "kb_save": "release_apply",
    "confluence_publish": "release_apply",
    "finalize": "release_apply",
}

# The key each gated node's own result dict is nested under -- release_planning
# is the one place this diverges from the node name itself.
RESULT_KEY = {
    "requirement": "requirement",
    "design": "design",
    "quality_planning": "quality_plan",
    "release_planning": "release_plan",
}

# Friendly names for each pipeline step, shown in status messages.
NODE_DISPLAY = {
    "requirement": "Requirement",
    "design": "Design",
    "quality_planning": "Quality Planner",
    "release_planning": "Engineering Manager",
    "release_apply": "Saving files",
    "jira_publish": "Jira",
    "kb_save": "Knowledge base",
    "confluence_publish": "Confluence",
    "finalize": "Done",
}

# Jira's own priority -> color convention, reused for the ticket badges
# in the Release stage so a Highest/High ticket reads as urgent at a
# glance, same as it would in a real Jira backlog.
PRIORITY_BADGE_COLOR = {
    "Highest": "red",
    "High": "orange",
    "Medium": "blue",
    "Low": "gray",
    "Lowest": "gray",
}

# Human-readable labels for the backend's internal outcome codes -- shown to
# a non-engineering audience, so SCREAMING_SNAKE_CASE needs a plain-English gloss.
OUTCOME_DISPLAY = {
    "RELEASED_TO_JIRA": "Approved & created in Jira",
    "JIRA_FAILED": "Saved locally -- creating the Jira tickets failed",
    "APPROVED_RELEASED": "Approved -- backlog saved locally (not sent to Jira)",
    "NO_CHANGE_REQUIRED": "No change required",
    "INCOMPLETE": "Incomplete",
    "UNKNOWN": "Unknown",
}


def _humanize_outcome(value: str) -> str:
    # Turn a code like "NO_CHANGE_REQUIRED" into readable text.
    return OUTCOME_DISPLAY.get(value, value.replace("_", " ").title())

# Maps each step name to the function that runs it. The UI calls these
# one at a time (rather than running the whole graph at once) so it can
# pause between stages.
NODE_FN = {
    "requirement": graph.requirement_node,
    "design": graph.design_node,
    "quality_planning": graph.quality_planning_node,
    "release_planning": graph.release_planning_node,
    "release_apply": graph.release_apply_node,
    "jira_publish": graph.jira_publish_node,
    "kb_save": graph.kb_save_node,
    "confluence_publish": graph.confluence_publish_node,
    "finalize": graph.finalize_node,
}

# Stages worth pausing on in human-in-the-loop mode -- release_apply
# and finalize are excluded because they only execute a decision
# (release_planning's plan) that was already gated one step earlier.
GATED_NODE_NAMES = {"requirement", "design", "quality_planning", "release_planning"}

# Same set as GATED_NODE_NAMES, but ordered -- used to populate the
# override gate's "send to" dropdown in a stable, pipeline-shaped order.
OVERRIDE_TARGETS = ["requirement", "design", "quality_planning", "release_planning"]

# What each node hands off to next. Reuses the graph's own router
# functions as the source of truth so this can't drift out of sync
# with the real routing logic.
NEXT_NODE_FN = {
    "requirement": graph.requirement_router,
    "design": graph.design_router,
    "quality_planning": lambda s: "release_planning",
    "release_planning": lambda s: "release_apply",
    "release_apply": lambda s: "jira_publish",
    "jira_publish": lambda s: "kb_save",
    "kb_save": lambda s: "confluence_publish",
    "confluence_publish": lambda s: "finalize",
    "finalize": lambda s: None,
}


# ============================================================
# RENDER HELPERS
# ============================================================

def render_pipeline(completed_node: str = None) -> None:
    # Draw the row of stage badges: blue with a tick = finished, green =
    # the stage just completed, grey = still to come.
    completed_key = STAGE_ALIASES.get(completed_node, completed_node)
    cols = st.columns(len(STAGE_META))
    seen = False
    for col, (key, label, icon) in zip(cols, STAGE_META):
        with col:
            if key == completed_key:
                st.badge(label, icon=icon, color="green")
                seen = True
            elif not seen:
                st.badge(label, icon=":material/check:", color="blue")
            else:
                st.badge(label, color="gray")


def render_clarifying_questions(stage_result: dict) -> None:
    # Show the AI's open questions in a collapsible box (read-only).
    questions = stage_result.get("clarifying_questions", [])
    if not questions:
        return
    with st.expander(f"Open questions ({len(questions)})", expanded=False):
        for q in questions:
            st.markdown(f"**{q.get('question', '')}**")
            if q.get("why_it_matters"):
                st.caption(q["why_it_matters"])
            for opt in q.get("options", []):
                marker = " (recommended)" if opt == q.get("recommended_option") else ""
                st.write(f"- {opt}{marker}")


def _apply_requirement_edit(edited: dict) -> None:
    # Save the reworded requirement into the run (so the Design agent and the downloads
    # use it) and refresh what is shown for the requirement stage.
    try:
        update = graph.apply_requirement_edits(st.session_state.pipeline_state, edited)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()
    st.session_state.pipeline_state.update(update)
    if "requirement" in update:
        events_list = st.session_state.completed_events
        last = max((i for i, (n, _) in enumerate(events_list) if n == "requirement"), default=None)
        if last is not None:
            events_list[last] = ("requirement", {**events_list[last][1], "requirement": edited})


def _same_or_new(old, new_text: str):
    # Typing the same words (ignoring spaces at the ends) is not an edit: keep the original.
    return old if (old or "").strip() == new_text.strip() else new_text.strip()


def render_requirement_editor(req: dict, edit_id: int) -> None:
    """Boxes to reword the requirement: title, scope, acceptance criteria, out of scope.
    The edits are saved by "Save wording changes", or automatically when the person
    presses Approve at the pause. A title, scope and at least one criterion are required."""
    key = lambda name: f"requirement_edit_{edit_id}_{name}"
    with st.expander("✎ Edit the wording of this requirement", expanded=False):
        edited = copy.deepcopy(req)
        edited["title"] = _same_or_new(req.get("title"), st.text_input(
            "Title", value=req.get("title", ""), key=key("title")))
        edited["scope_summary"] = _same_or_new(req.get("scope_summary"), st.text_area(
            "Scope", value=req.get("scope_summary", ""), height=120, key=key("scope")))
        criteria = _lines(st.text_area(
            "Acceptance criteria (one per line)", value="\n".join(req.get("acceptance_criteria", [])),
            height=140, key=key("criteria")))
        edited["acceptance_criteria"] = (
            req.get("acceptance_criteria") if criteria == _lines("\n".join(req.get("acceptance_criteria", []))) else criteria)
        out_of_scope = _lines(st.text_area(
            "Out of scope (one per line)", value="\n".join(req.get("out_of_scope", [])),
            height=100, key=key("out_of_scope")))
        edited["out_of_scope"] = (
            req.get("out_of_scope") if out_of_scope == _lines("\n".join(req.get("out_of_scope", []))) else out_of_scope)

        changes = graph.requirement_text_changes(req, edited)
        problems = (
            (["a title"] if not edited["title"].strip() else [])
            + (["a scope"] if not edited["scope_summary"].strip() else [])
            + (["at least one acceptance criterion"] if not edited["acceptance_criteria"] else [])
        )
        if problems:
            st.error("The requirement needs " + ", ".join(problems) + ". Fix this to keep your changes.")
        elif changes:
            st.caption(
                f"✎ {len(changes)} wording edit(s). They are saved when you press Freeze requirements, "
                "or press Save now. (Using Reconsider instead has the agent rewrite the "
                "requirement, which replaces these edits.)"
            )
        # Remembered for the Approve button, which saves them and refuses while they are invalid.
        st.session_state["_pending_requirement_edit"] = edited if (changes and not problems) else None
        st.session_state["_requirement_edit_invalid"] = bool(problems)
        if st.button("Save wording changes", key=key("save"), disabled=bool(problems) or not changes):
            _apply_requirement_edit(edited)
            st.rerun()


@st.cache_data(show_spinner=False)
def _diagram_images(design_json: str, requirement_json: str, edits_json: str) -> dict:
    # The diagrams are drawn once per distinct design + wording, not on every re-run of the page.
    return design_doc.build_images(json.loads(design_json), json.loads(requirement_json), json.loads(edits_json))


def _apply_design_edits(edits: dict) -> None:
    # Save the user's wording changes to the design document (the design itself is not touched).
    try:
        update = graph.apply_design_doc_edits(st.session_state.pipeline_state, edits)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()
    st.session_state.pipeline_state.update(update)


def render_design_wording_editor(design: dict, req: dict, edit_id: int) -> None:
    """Boxes to reword the design document: its title, overview and every description, label and
    note. Names, kinds, statuses and who-talks-to-whom are NOT editable here (that is structure:
    use "Revise the design" to have the Design agent change it). Saved by "Save wording changes",
    or automatically when the person approves; an empty required field blocks both."""
    state = st.session_state.pipeline_state
    current = state.get("design_doc_edits") or {}
    try:
        fields = design_doc.editable_fields(design, req, current)
    except ValueError:                      # edits that no longer fit this design: start from the original
        current, fields = {}, design_doc.editable_fields(design, req, {})
    key = lambda path: f"design_edit_{edit_id}_{path}"
    proposed = {}
    with st.expander("✎ Edit the wording of the design document", expanded=False):
        st.caption(
            "Change the words only. Which components exist and how they connect stay as the Design agent "
            "made them; to change those, use “Revise the design” at the pause. Edits show in the document, "
            "the diagrams' labels, the downloads and the Confluence page."
        )
        section = None
        for f in fields:
            if f["section"] != section:
                section = f["section"]
                st.markdown(f"**{section}**")
            if f["kind"] == "line":
                proposed[f["path"]] = st.text_input(f["label"], value=f["value"], key=key(f["path"]))
            elif f["kind"] == "text":
                height = max(68, min(240, 30 + 22 * (len(f["value"]) // 80 + 1)))
                proposed[f["path"]] = st.text_area(f["label"], value=f["value"], height=height, key=key(f["path"]))
            else:
                proposed[f["path"]] = _lines(st.text_area(
                    f["label"], value="\n".join(f["value"]), height=90, key=key(f["path"])))
        try:
            kept, problem = design_doc.normalise_edits(design, req, proposed), ""
        except ValueError as exc:
            kept, problem = None, str(exc)
        unsaved = kept is not None and kept != current
        if problem:
            st.error(problem + ". Fix this to keep your changes.")
        elif unsaved:
            st.caption("✎ Unsaved wording changes. They are saved when you approve, or press Save now.")
        st.session_state["_pending_design_edits"] = kept if unsaved else None
        st.session_state["_design_edit_invalid"] = bool(problem)
        if st.button("Save wording changes", key=key("save"), disabled=bool(problem) or not unsaved):
            _apply_design_edits(kept)
            st.rerun()


def render_design_document(design: dict, req: dict, editable: bool = False, edit_id: int = 0,
                           final: bool = False) -> None:
    """The Design agent's output as a design document: overview, architecture diagram, tables of
    components and interconnections, flow diagrams, build order and the rest - shown here exactly
    as it will appear in the downloads and on the Confluence page. The person may reword it, and
    download it as Word or PDF."""
    state = st.session_state.get("pipeline_state", {})
    req = req or {}
    edits = state.get("design_doc_edits") or {}
    direction = (state.get("architecture_direction") or "").strip()
    if direction:
        st.caption(f"Following your architectural direction: {direction}")
    try:
        meta = graph.doc_meta(state)
        model = design_doc.build_model(req, design, meta, edits)
        images = _diagram_images(json.dumps(design, sort_keys=True, default=str),
                                 json.dumps(req, sort_keys=True, default=str), json.dumps(edits, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 -- a document problem must never break the page
        st.error(f"Could not build the design document: {exc}")
        return

    st.markdown(f"### {model['title']}")
    st.caption("  ·  ".join(f"{k}: {v}" for k, v in model["meta"]))
    for b in model["blocks"]:
        kind = b["type"]
        if kind == "h1":
            st.markdown(f"#### {b['text']}")
        elif kind == "h2":
            st.markdown(f"**{b['text']}**")
        elif kind == "p":
            if b.get("style") == "bold":
                st.markdown(f"**{b['text']}**")
            elif b.get("style") == "muted":
                st.caption(b["text"])
            else:
                st.write(b["text"])
        elif kind == "bullets":
            st.markdown("\n".join(f"- {i}" for i in b["items"]))
        elif kind == "numbered":
            st.markdown("\n".join(f"{label} {text}" for label, text in b["items"]))
        elif kind == "table":
            st.markdown(design_doc.table_markdown(b, rich=True))
        elif kind == "image" and b["key"] in images:
            st.image(images[b["key"]], caption=b["caption"], use_container_width=True)

    # Proposed knowledge-base documents (nothing is saved until the end).
    kb_updates = design.get("knowledge_base_updates", [])
    if kb_updates:
        with st.expander(f"Knowledge base changes ({len(kb_updates)})", expanded=False):
            st.caption("Nothing is saved yet. At the end of the release you'll be asked whether to save "
                       "these — as a new knowledge base, or into the one you selected.")
            for u in kb_updates:
                st.markdown(f"**{'Create' if u.get('action') == 'create' else 'Append to'}** `{u.get('application', '')}`")
                if u.get("reason"):
                    st.caption(u["reason"])
                st.code(u.get("content", ""), language="markdown")

    if not final:
        st.caption("Draft — discuss it, ask for changes or reword it below. When you are happy, freeze the "
                   "architecture to download it or publish it to Confluence.")
        if editable:
            render_design_wording_editor(design, req, edit_id)
        with st.expander("Raw data (JSON)", expanded=False):
            st.json(design)
        return
    st.caption("Download this design as a document — pick the format you prefer.")
    columns = st.columns(len(design_doc.FORMATS))
    for column, fmt in zip(columns, design_doc.FORMATS):
        label = design_doc.FORMATS[fmt][0]
        try:
            data, file_name, mime = design_doc.build(req, design, meta, edits, fmt)
        except design_doc.MissingLibrary as exc:
            column.caption(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001
            column.caption(f"Could not make the {label}: {exc}")
            continue
        offer_download(column, f"Download design as {label}", data, file_name, mime,
                       f"download_design_{next(_DOWNLOAD_KEYS)}")
    with st.expander("Raw data (JSON)", expanded=False):
        st.json(design)


def render_freeze_confluence(state: dict, gate_n: int) -> None:
    """In the frozen-architecture box: publish the design to Confluence now, if it is set up."""
    design = state.get("design") or {}
    if not (design.get("change_required") and design.get("components")):
        return
    if not confluence_client.is_configured():
        st.caption("Confluence is not set up, so the document can only be downloaded.")
        return
    result = state.get("confluence_result") or {}
    _, page_title = design_doc.effective(design, state.get("requirement") or {}, state.get("design_doc_edits"))
    cf = confluence_client.preview(page_title, len(design_doc.attachment_names(design)))
    st.caption(f"Confluence: {cf['base_url']} · space {cf['space_key']} · page “{page_title}” · "
               f"{cf['attachments']} diagram(s) attached. An existing page with this title is never overwritten.")
    published = result.get("status") == "published"
    if st.button("Publish again to Confluence" if published else "Publish to Confluence",
                 key=f"freeze_cf_{gate_n}", use_container_width=True):
        with st.spinner("Publishing to Confluence…"):
            state.update(graph.confluence_publish_node({**state, "confluence_approved": True, "confluence_result": {}}))
        st.rerun()
    if result:
        render_confluence_result(result)


def _apply_quality_edit(edited: dict) -> None:
    # Save the edited test plan into the run and refresh what is shown for the quality stage.
    try:
        update = graph.apply_quality_plan_edits(st.session_state.pipeline_state, edited)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()
    st.session_state.pipeline_state.update(update)
    if "quality_plan" in update:
        events_list = st.session_state.completed_events
        last = max((i for i, (n, _) in enumerate(events_list) if n == "quality_planning"), default=None)
        if last is not None:
            events_list[last] = ("quality_planning", {**events_list[last][1], "quality_plan": edited})


def render_quality_editor(plan: dict, req: dict, edit_id: int) -> None:
    """Boxes to edit the test plan: its summary and every field of every test case, plus removing
    a case or adding a new one. Saved by "Save test plan changes", or automatically when the
    person freezes the plan. Every case needs its text, and every acceptance criterion must stay
    covered by at least one case."""
    key = lambda *parts: "quality_edit_" + str(edit_id) + "_" + "_".join(str(p) for p in parts)
    labels = list(quality_agent.criteria_ids(req or {}))
    with st.expander("✎ Edit the test plan", expanded=False):
        edited = copy.deepcopy(plan)
        edited["summary"] = st.text_area("Approach summary", value=plan.get("summary", ""),
                                         height=100, key=key("summary"))
        kept = []
        for c in plan.get("test_cases", []):
            cid = c.get("id")
            with st.container(border=True):
                head, drop = st.columns([5, 2])
                head.markdown(f"**{cid}**")
                if drop.checkbox("Remove this case", key=key(cid, "remove")):
                    continue
                n = copy.deepcopy(c)
                n["title"] = st.text_input("Title", value=c.get("title", ""), key=key(cid, "title"))
                n["component"] = st.text_input("Component", value=c.get("component", ""), key=key(cid, "component"))
                t_col, p_col = st.columns(2)
                n["type"] = t_col.selectbox(
                    "Type", quality_agent.CASE_TYPES, key=key(cid, "type"),
                    index=quality_agent.CASE_TYPES.index(c["type"]) if c.get("type") in quality_agent.CASE_TYPES else 0)
                n["priority"] = p_col.selectbox(
                    "Priority", quality_agent.PRIORITIES, key=key(cid, "priority"),
                    index=quality_agent.PRIORITIES.index(c["priority"]) if c.get("priority") in quality_agent.PRIORITIES else 0)
                n["requirement_refs"] = st.multiselect(
                    "Covers acceptance criteria", labels, key=key(cid, "refs"),
                    default=[r for r in c.get("requirement_refs") or [] if r in labels])
                n["preconditions"] = st.text_area("Before you start", value=c.get("preconditions") or "",
                                                  height=68, key=key(cid, "pre"))
                n["steps"] = _lines(st.text_area("Steps (one per line)", value="\n".join(c.get("steps") or []),
                                                 height=130, key=key(cid, "steps")))
                n["test_data"] = st.text_area("Test data", value=c.get("test_data") or "",
                                              height=68, key=key(cid, "data"))
                n["expected_result"] = st.text_area("Expected result", value=c.get("expected_result", ""),
                                                    height=90, key=key(cid, "expected"))
                kept.append(n)
        edited["test_cases"] = kept

        try:
            graph.apply_quality_plan_edits({**st.session_state.pipeline_state, "quality_plan": {}}, edited)
            problem = ""
        except ValueError as exc:
            problem = str(exc)
        changed = edited != plan
        if problem:
            st.error(problem + " Fix this to keep your changes.")
        elif changed:
            st.caption("✎ Unsaved test plan changes. They are saved when you freeze the test plan, "
                       "or press Save now. (Using Reconsider instead has the agent rewrite the plan, "
                       "which replaces these edits.)")
        st.session_state["_pending_quality_edit"] = edited if (changed and not problem) else None
        st.session_state["_quality_edit_invalid"] = bool(problem)
        save_col, add_col = st.columns(2)
        if save_col.button("Save test plan changes", key=key("save"), disabled=bool(problem) or not changed):
            _apply_quality_edit(edited)
            st.rerun()
        if add_col.button("Add a test case", key=key("add"), disabled=bool(problem)):
            numbers = [int(m.group(1)) for c in edited["test_cases"] if (m := re.match(r"TC-(\d+)$", str(c.get("id"))))]
            edited["test_cases"].append({
                "id": f"TC-{max(numbers, default=0) + 1}", "title": "New test case",
                "component": (kept[0].get("component") if kept else "") or "General",
                "type": "Functional", "priority": "Medium",
                "requirement_refs": [], "preconditions": "", "steps": ["Describe the first step"],
                "test_data": "", "expected_result": "Describe the expected result",
            })
            _apply_quality_edit(edited)
            st.rerun()


def render_quality_plan(plan: dict, req: dict, final: bool = False, editable: bool = False, edit_id: int = 0) -> None:
    """The Quality Planner's test plan: an approach summary, the test cases as a table, and which
    cases cover which acceptance criterion. Once frozen it can be downloaded as an Excel workbook."""
    cases = plan.get("test_cases", [])
    st.write(plan.get("summary", ""))
    meta = {"run_id": st.session_state.get("run_id", "")}
    shown = test_plan_doc.counts(plan)
    st.caption(f"{len(cases)} test case(s) · " + " · ".join(f"{n} {name.lower()}" for name, n in shown["type"].items()))
    st.dataframe(
        [{"ID": c.get("id"), "Title": c.get("title"), "Component": c.get("component"), "Type": c.get("type"),
          "Priority": c.get("priority"), "Covers": ", ".join(c.get("requirement_refs") or [])} for c in cases],
        hide_index=True, use_container_width=True,
    )
    for c in cases:
        with st.expander(f"{c.get('id')} — {c.get('title')}", expanded=False):
            st.caption(f"{c.get('component')} · {c.get('type')} · {c.get('priority')} priority"
                       + (f" · covers {', '.join(c['requirement_refs'])}" if c.get("requirement_refs") else ""))
            if c.get("preconditions"):
                st.markdown(f"**Before you start:** {c['preconditions']}")
            st.markdown("**Steps**\n\n" + "\n".join(f"{i}. {step}" for i, step in enumerate(c.get("steps") or [], start=1)))
            if c.get("test_data"):
                st.markdown(f"**Test data:** {c['test_data']}")
            st.markdown(f"**Expected result:** {c.get('expected_result', '')}")
    with st.expander("Which test cases cover each acceptance criterion", expanded=False):
        for item in test_plan_doc.coverage(req or {}, plan):
            st.markdown(f"**{item['label']}** {item['criterion']}  \n"
                        + (", ".join(item["cases"]) if item["cases"] else ":red[not covered]"))

    if not final:
        st.caption("Draft — discuss it, ask for changes or edit it below. When you are happy, freeze the test plan "
                   "to download it as an Excel workbook.")
        if editable:
            render_quality_editor(plan, req, edit_id)
        return
    st.success("Test plan frozen. Download the Excel workbook, then continue.")
    st.caption("Download this test plan as a spreadsheet.")
    try:
        data, file_name, mime = test_plan_doc.build(req or {}, plan, meta)
    except test_plan_doc.MissingLibrary as exc:
        st.caption(str(exc))
    except Exception as exc:  # noqa: BLE001 -- a document problem must never break the page
        st.caption(f"Could not make the Excel workbook: {exc}")
    else:
        offer_download(st, f"Download {test_plan_doc.FORMATS['xlsx'][0]}", data, file_name, mime,
                       f"download_test_plan_{next(_DOWNLOAD_KEYS)}")
    with st.expander("Raw data (JSON)", expanded=False):
        st.json(plan)


def render_confluence_result(result: dict) -> None:
    # The outcome of the Confluence step: a link to the page, or why nothing was published.
    status = result.get("status")
    if status == "published":
        st.markdown(f"Published: [{result['title']}]({result['url']})")
        st.caption(
            f"Space {result['space_key']} · {len(result.get('attachments', []))} diagram(s) attached"
            + (" · updated in place (a new version)" if result.get("updated") else "")
            + (" · nothing had changed since it was last published" if result.get("unchanged") else "")
        )
    elif status == "failed":
        st.error(result.get("error", "Publishing to Confluence failed."))
        st.caption("The document is still available to download above. Fix the cause and retry; a retry "
                   "resumes without creating a second page.")
    else:
        st.info(result.get("reason", "Nothing was published."))


# The stages that end in a frozen, downloadable document: (button, what is frozen, the agent that follows).
FREEZE = {
    "requirement": ("Freeze requirements", "Requirements", "Design"),
    "design": ("Freeze architecture", "Architecture", "Quality Planner"),
    "quality_planning": ("Freeze test plan", "Test plan", "Engineering Manager"),
}


def stage_is_frozen(name: str) -> bool:
    """True while the person has frozen this stage's output at the current pause."""
    return st.session_state.get(f"{name}_frozen_at") == len(st.session_state.completed_events)


def stage_shows_document(name: str) -> bool:
    """Whether the finished document (with its downloads) is shown for this stage rather than the
    draft: once frozen, once the run has moved past the stage, or in automatic mode (no pause)."""
    events = st.session_state.completed_events
    return (stage_is_frozen(name) or not st.session_state.human_in_the_loop
            or not events or events[-1][0] != name)


def render_requirement_document(req: dict, editable: bool = False, edit_id: int = 0, final: bool = False) -> None:
    """The Requirement agent's output as a document: shown here, and downloadable as
    Word or PDF - whichever the person prefers. The files are made on this computer.
    If `editable`, the person can also reword it (only possible before the design exists)."""
    state = st.session_state.get("pipeline_state", {})
    meta = {
        "run_id": st.session_state.get("run_id", ""),
        # The knowledge base this run actually used (none when it started from scratch).
        "knowledge_base": "" if state.get("from_scratch") else state.get("kb_name", ""),
    }
    st.markdown(requirement_doc.to_markdown(req, meta, compact=True))

    if not final:
        # Still being worked on: no document yet. It is produced when the person freezes the requirements.
        st.caption("Draft — discuss it, answer the questions or reword it below. When you are happy, "
                   "freeze the requirements to get the document to download.")
        if editable:
            render_requirement_editor(req, edit_id)
        with st.expander("Raw data (JSON)", expanded=False):
            st.json(req)
        return
    st.success("Requirements frozen. Download the document in the format you prefer, then continue.")
    st.caption("Download this requirement as a document — pick the format you prefer.")
    columns = st.columns(len(requirement_doc.FORMATS))
    for column, fmt in zip(columns, requirement_doc.FORMATS):
        label = requirement_doc.FORMATS[fmt][0]
        try:
            data, file_name, mime = requirement_doc.build(req, meta, fmt)
        except requirement_doc.MissingLibrary as exc:
            column.caption(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 -- a document problem must never break the page
            column.caption(f"Could not make the {label}: {exc}")
            continue
        # on_click="ignore": clicking must not re-run the page, which could interrupt
        # the stage that is running underneath.
        offer_download(column, f"Download {label}", data, file_name, mime,
                       f"download_requirement_{next(_DOWNLOAD_KEYS)}")
    with st.expander("Raw data (JSON)", expanded=False):
        st.json(req)


def _lines(text: str) -> list:
    # Text typed one-item-per-line -> a list of the non-empty lines.
    return [line.strip() for line in text.splitlines() if line.strip()]


def _reword_steps(old_steps: list, text: str) -> list:
    # Turn the "steps" text box back into a list of steps. A line that matches an
    # existing step's title keeps that step exactly as it was (including any extra
    # detail); a new line becomes a new step with just that title.
    by_title = {step["title"]: step for step in old_steps}
    return [copy.deepcopy(by_title[line]) if line in by_title else {"title": line} for line in _lines(text)]


def render_jira_result(jira: dict) -> None:
    # Show the outcome of the Jira step: created (with clickable links),
    # not sent (declined / not configured), or failed (with the reason).
    status = jira.get("status")
    if status == "created":
        c = jira["counts"]
        st.write(
            f"Created in project **{jira['project_key']}** — {c['epics']} epic, "
            f"{c['stories']} stories, {c['sub_tasks']} sub-tasks"
            + (" (already created earlier; nothing was duplicated)" if jira.get("resumed") else "")
        )
        epic = jira.get("epic")
        if epic:
            st.markdown(f"**Epic:** [{epic['key']}]({epic['url']}) — {epic['title']}")
        for story in jira["stories"]:
            st.markdown(f"- [{story['key']}]({story['url']}) {story['title']} _({story['component']})_")
            for task in story["tasks"]:
                st.markdown(f"  - [{task['key']}]({task['url']}) {task['title']}")
        left = jira.get("not_sent") or {}
        if left.get("stories") or left.get("sub_tasks") or left.get("epic"):
            parts = []
            if left.get("epic"):
                parts.append("the epic")
            if left.get("stories"):
                parts.append(f"{len(left['stories'])} stories")
            if left.get("sub_tasks"):
                parts.append(f"{len(left['sub_tasks'])} sub-tasks")
            st.caption("Not sent to Jira (not selected): " + ", ".join(parts) + ". They remain in the local files.")
        for d in jira.get("dropped_fields", []):
            st.caption(f"Left out `{d['field']}`: this project would not accept it ({d['reason']}).")
        for w in jira.get("warnings", []):
            st.warning(w)
        st.caption(jira.get("mapping", ""))
    elif status == "failed":
        st.error(jira.get("error", "Creating the Jira tickets failed."))
        st.caption(
            f"{jira.get('created_count', 0)} issue(s) were created before the failure. "
            "Retrying the same plan resumes where it stopped, without duplicates."
        )
    else:
        st.info(jira.get("reason", "Nothing was sent to Jira."))


def render_story_tree(stories) -> None:
    # Draw the Jira backlog: one card per story (priority / points badges,
    # description, acceptance criteria), then its tasks with their subtasks.
    # Works both for the plan (stories have "id") and for the published
    # issues (stories have "ticket_id").
    for story in stories:
        key = story.get("ticket_id") or story.get("id", "")
        with st.container(border=True):
            st.markdown(f"**{key}: {story.get('title', '')}**")

            badge_cols = st.columns(4)
            with badge_cols[0]:
                st.badge("Story", icon=":material/bookmark:", color="green")
            with badge_cols[1]:
                priority = story.get("priority", "")
                st.badge(priority, color=PRIORITY_BADGE_COLOR.get(priority, "gray"))
            with badge_cols[2]:
                st.badge(f"{story.get('story_points', '?')} pts", color="blue")
            with badge_cols[3]:
                st.caption(story.get("component", ""))

            if story.get("labels"):
                st.caption("Labels: " + ", ".join(story["labels"]))
            if story.get("description"):
                st.write(story["description"])

            criteria = story.get("acceptance_criteria", [])
            if criteria:
                with st.expander(f"Acceptance Criteria ({len(criteria)})", expanded=False):
                    for criterion in criteria:
                        st.write(f"- [ ] {criterion}")

            tasks = story.get("tasks", [])
            subtask_count = sum(len(t.get("subtasks", [])) for t in tasks)
            with st.expander(f"Tasks ({len(tasks)}) · Subtasks ({subtask_count})", expanded=False):
                for task in tasks:
                    task_key = task.get("ticket_id", "")
                    st.markdown(f"**{task_key + ': ' if task_key else ''}{task.get('title', '')}**")
                    if task.get("description"):
                        st.caption(task["description"])
                    for sub in task.get("subtasks", []):
                        st.write(f"- [ ] {sub.get('title', '')}")

            blocked = (
                f"Is blocked by: {', '.join(story['blocked_by'])}"
                if story.get("blocked_by")
                else "Not blocked"
            )
            st.caption(blocked)


def stage_label(node_name: str, node_result: dict, pass_number: int = 1) -> str:
    """The informative, final label for a completed stage -- used as
    the st.status() title once its result is known. pass_number counts
    how many times this stage has run in this session (human-requested
    revisions increment it)."""
    suffix = f" — pass {pass_number}" if pass_number > 1 else ""

    # Each branch builds the heading for one stage, adding a useful hint
    # (e.g. the title, or "change required").
    if node_name == "requirement":
        if node_result.get("requirement_skipped"):
            return f"Requirement{suffix} — used as written (agent skipped)"
        title = node_result.get("requirement", {}).get("title", "")
        return f"Requirement{suffix} — {title}" if title else f"Requirement{suffix}"

    if node_name == "quality_planning":
        n = len(node_result.get("quality_plan", {}).get("test_cases") or [])
        return f"Quality Planner{suffix} — {n} test case(s)"

    if node_name == "design":
        design = node_result.get("design", {})
        tag = "change required" if design.get("change_required") else "no change needed"
        n = len(design.get("components") or [])
        return f"Design{suffix} — {tag}" + (f" ({n} components)" if n else "")

    if node_name == "release_planning":
        plan = node_result.get("release_plan", {})
        title = plan.get("epic", {}).get("title", "")
        n = len(plan.get("stories", []))
        head = f"Engineering Manager{suffix}"
        return f"{head} — {title} ({n} stories)" if title else head

    if node_name == "release_apply":
        return "Backlog saved locally"

    if node_name == "confluence_publish":
        status = node_result.get("confluence_result", {}).get("status")
        return {
            "published": "Confluence — published",
            "declined": "Confluence — not published (no approval)",
            "skipped": "Confluence — skipped",
            "failed": "Confluence — failed",
        }.get(status, "Confluence")

    if node_name == "kb_save":
        kb = node_result.get("kb_result", {})
        status = kb.get("status")
        if status == "saved":
            return f"Knowledge base — saved to {kb.get('kb_name')}"
        return {
            "declined": "Knowledge base — not saved",
            "none": "Knowledge base — nothing to save",
            "failed": "Knowledge base — save failed",
        }.get(status, "Knowledge base")

    if node_name == "jira_publish":
        jira = node_result.get("jira_result", {})
        status = jira.get("status")
        if status == "created":
            c = jira["counts"]
            return f"Jira — created {c['epics'] + c['stories'] + c['sub_tasks']} issue(s)"
        return {
            "declined": "Jira — not sent (no approval)",
            "skipped": "Jira — skipped (not configured)",
            "failed": "Jira — failed",
        }.get(status, "Jira")

    if node_name == "finalize":
        return f"Done — {_humanize_outcome(node_result.get('run_outcome', 'UNKNOWN'))}"

    return NODE_DISPLAY.get(node_name, node_name)


def stage_state(node_name: str, node_result: dict) -> str:
    """The st.status() state for a completed stage (always a green tick:
    a stage that finishes has succeeded; failures are shown separately as
    a pipeline error)."""
    return "complete"


def render_stage_body(node_name: str, node_result: dict, editable: bool = False, edit_id: int = 0) -> None:
    """Write one stage's detailed content. Must be called from inside
    an open st.status(...) block, which supplies the header/icon. `editable` (used
    only for the requirement) shows the boxes for rewording it."""

    # Each section below shows one stage's result in a readable layout.
    #
    # REQUIREMENT: short scope summary, with full details tucked away.
    # ------------------------------------------------
    if node_name == "requirement":
        req = node_result.get("requirement", {})
        if node_result.get("requirement_skipped"):
            st.caption("The Requirement agent was skipped: your text is used as the requirement, exactly as written.")
            st.write(req.get("scope_summary", ""))
        else:
            final = stage_shows_document("requirement")
            render_requirement_document(req, editable=editable and not final, edit_id=edit_id, final=final)

    # DESIGN: affected apps (left), unaffected apps (right), build order,
    # test strategy, contracts, risks, knowledge-base changes, diagrams.
    # ------------------------------------------------
    elif node_name == "design":
        design = node_result.get("design", {})
        render_design_document(design, st.session_state.get("pipeline_state", {}).get("requirement") or {},
                               editable=editable and not stage_shows_document("design"), edit_id=edit_id,
                               final=stage_shows_document("design"))

    # QUALITY PLANNING: the plain-English test plan (downloadable as Excel once frozen).
    # ------------------------------------------------
    elif node_name == "quality_planning":
        plan = node_result.get("quality_plan", {})
        render_quality_plan(plan, st.session_state.get("pipeline_state", {}).get("requirement") or {},
                            final=stage_shows_document("quality_planning"),
                            editable=editable and not stage_shows_document("quality_planning"), edit_id=edit_id)
        render_clarifying_questions(plan)

    # RELEASE PLANNING: rollout story, the backlog (stories > tasks >
    # subtasks), and each rollout stage's go/no-go criteria and rollback plan.
    # ------------------------------------------------
    elif node_name == "release_planning":
        plan = node_result.get("release_plan", {})
        st.write(plan.get("rollout_narrative", ""))

        st.markdown(f"**Epic:** {plan.get('epic', {}).get('title', '')}")
        render_story_tree(plan.get("stories", []))

        stages = plan.get("rollout_stages", [])
        if stages:
            with st.expander(f"Staged rollout — {len(stages)} stage(s)", expanded=False):
                for stage in stages:
                    with st.container(border=True):
                        st.markdown(f"**Stage {stage['stage']}**")
                        st.caption(", ".join(stage.get("applications", [])))
                        st.write("Go/No-Go criteria:")
                        for c in stage.get("go_no_go_criteria", []):
                            st.write(f"- {c}")
                        st.write(f"Rollback: {stage.get('rollback_plan', '')}")
                        if not stage.get("rollback_safe", True):
                            st.badge("Not safely rollback-able", color="red")
        render_clarifying_questions(plan)

    # RELEASE APPLY (publishing): the epic with its stories / tasks /
    # subtasks as written to Jira, and what happened to the knowledge base.
    # ------------------------------------------------
    elif node_name == "release_apply":
        result = node_result.get("release_result", {})
        counts = result.get("counts", {})
        st.write(
            f"Epic: **{result.get('epic_id')}** — {counts.get('stories', 0)} story(ies), "
            f"{counts.get('tasks', 0)} task(s), {counts.get('subtasks', 0)} subtask(s)"
        )
        render_story_tree(result.get("stories", []))


    # ------------------------------------------------
    # KNOWLEDGE BASE SAVE: the user's answer to "save the knowledge base?" and,
    # if they said yes, what was written where.
    # ------------------------------------------------
    elif node_name == "kb_save":
        kb = node_result.get("kb_result", {})
        if kb.get("status") == "saved":
            what = "new knowledge base" if kb.get("created_new_kb") else "knowledge base"
            st.write(f"Saved to {what} **{kb['kb_name']}** (`{kb['path']}`)")
            for r in kb.get("results", []):
                mark = {"applied": "✓", "skipped": "–", "failed": "✗"}.get(r.get("status"), "•")
                detail = f" — {r['detail']}" if r.get("detail") else ""
                st.write(f"- {mark} {r.get('action', '')} `{r.get('application', '')}`{detail}")
        elif kb.get("status") == "failed":
            st.error(kb.get("error", "Saving the knowledge base failed."))
        else:
            st.info(kb.get("reason", "Nothing was saved."))

    # CONFLUENCE: the design page published (with a link), or why it was not.
    # ------------------------------------------------
    elif node_name == "confluence_publish":
        render_confluence_result(node_result.get("confluence_result", {}))

    # ------------------------------------------------
    # JIRA PUBLISH: what happened when we tried to create the tickets in the
    # real Jira - links to every ticket, or why nothing was sent.
    # ------------------------------------------------
    elif node_name == "jira_publish":
        render_jira_result(node_result.get("jira_result", {}))

    # ------------------------------------------------
    # FINALIZE: the final outcome in plain English.
    # ------------------------------------------------
    elif node_name == "finalize":
        st.write(f"Outcome: **{_humanize_outcome(node_result.get('run_outcome', 'UNKNOWN'))}**")


def _pass_number(node_name: str, events) -> int:
    # How many times has this stage run so far in this session? (A retry
    # makes it 2, 3, ...)
    return sum(1 for n, _ in events if n == node_name)


# ============================================================
# HIVE PANEL (sidebar): live event log and blackboard
# ============================================================

# Small symbol shown next to each kind of event in the log.
EVENT_ICON = {"stage_started": "▶", "stage_done": "✓", "stage_failed": "✗",
              "human_note": "👤"}


def render_hive(run_id: str) -> None:
    # Draw the sidebar: the blackboard sections and the event log.
    st.markdown("### Hive")

    # One collapsible list per blackboard section, showing only entries
    # that are still active. "Human decisions" opens by default if non-empty.
    board = blackboard.read(run_id)
    for section, title in (("human_decisions", "Human decisions"), ("open_questions", "Open questions"),
                           ("risks", "Risks"), ("decisions", "Decisions")):
        active = [e for e in board[section] if e["status"] == "active"]
        with st.expander(f"{title} ({len(active)})", expanded=section == "human_decisions" and bool(active)):
            for e in active:
                st.markdown(f"- **{e['stage']}**: {e['text']}")
            if not active:
                st.caption("Nothing yet.")

    # The 40 most recent events, newest first, with time (HH:MM:SS).
    with st.expander("Event log", expanded=False):
        for ev in reversed(events.read(run_id)[-40:]):
            detail = ev.get("summary") or ev.get("reason") or ev.get("note") or ev.get("error") or ""
            st.caption(
                f"{ev['ts'][11:19]} {EVENT_ICON.get(ev['kind'], '•')} "
                f"{ev.get('stage') or ''} {ev['kind']} {detail}"
            )


# ============================================================
# LIVE "WORKING" STATUS
#
# Each stage is one blocking LLM call that can take tens of seconds,
# during which a plain st.status(state="running") shows nothing but a
# static label -- no visible sign the agent hasn't just hung. This
# runs that call on a background thread while the main thread updates
# the status label every couple seconds with a rotating, Claude-Code
# -style whimsical verb + what it's plausibly chewing on, in the spirit
# of the CLI's own spinner ("Discombobulating...", "Spelunking...").
# Purely cosmetic -- the worker thread never touches Streamlit itself,
# only the main thread (already in-script) calls status_box.update().
# ============================================================

# Playful phrases rotated on screen while the AI is thinking.
WORKING_VERBS = [
    ("Spelunking", "the knowledge base"),
    ("Discombobulating", "the dependency order"),
    ("Percolating", "cross-service contracts"),
    ("Noodling on", "the requirement"),
    ("Wrangling", "affected applications"),
    ("Cross-examining", "architecture risks"),
    ("Untangling", "the rollout sequence"),
    ("Marinating", "test scenarios"),
    ("Sifting through", "the design risks"),
    ("Reticulating", "the release plan"),
    ("Puzzling over", "acceptance criteria"),
    ("Ruminating over", "the implementation specs"),
]


def _run_with_live_status(status_box, base_label: str, fn, *args, **kwargs):
    """Run `fn(*args, **kwargs)` (a slow AI call) in the background while
    the page keeps updating a "working..." label, then return its result
    (or raise its error)."""
    # Shared "mailboxes" the background worker uses to hand back its
    # result or its error, plus a flag that says "finished".
    result_box = {}
    error_box = {}
    done = threading.Event()

    def _worker():
        # Runs on the background thread: do the slow work, capture the
        # outcome, then signal that we are done.
        try:
            result_box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread below
            error_box["error"] = exc
        finally:
            done.set()

    # Start the background worker.
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    # Meanwhile, every ~1.6 seconds refresh the label with a new playful
    # phrase and the elapsed seconds, until the worker signals completion.
    verbs = random.sample(WORKING_VERBS, len(WORKING_VERBS))
    start = time.monotonic()
    i = 0
    while not done.wait(timeout=1.6):
        elapsed = int(time.monotonic() - start)
        verb, subject = verbs[i % len(verbs)]
        status_box.update(label=f"{base_label} — {verb} {subject}… ({elapsed}s)")
        i += 1

    thread.join()
    # If the worker failed, raise the same error here so callers see it.
    if "error" in error_box:
        raise error_box["error"]
    return result_box["value"]


# ============================================================
# RUN STATE MACHINE
#
# Streamlit reruns this whole script on every interaction (including
# an Approve/Suggest-changes click), so the pipeline is driven node by
# node directly (not via graph.build_graph().stream(), which can only
# advance forward) -- this is what lets a human-in-the-loop gate send
# a stage back to revise its own last output. The current node name
# and accumulated stage history are kept in st.session_state across
# reruns: each script execution advances as far as it can go without
# approval, then re-renders the full history so far, so nothing
# already shown ever disappears.
# ============================================================

def _is_gated(node: str) -> bool:
    # In Human approval mode, does the run pause after this stage? Yes for the
    # three main stages - except the Requirement stage when it was skipped (there
    # is nothing from the agent to review).
    if not st.session_state.human_in_the_loop or node not in GATED_NODE_NAMES:
        return False
    return not (node == "requirement" and st.session_state.pipeline_state.get("requirement_skipped"))


def _start_new_run(
    requirement_text: str, human_in_the_loop: bool, agent_models: dict, kb_name: str = "",
    start_at: str = "requirement",
) -> None:
    # Reset the per-visitor memory (session_state) for a brand-new run.
    run_id = graph.new_run_id()
    st.session_state.run_started = True
    st.session_state.run_active = True
    st.session_state.run_id = run_id
    st.session_state.requirement_text = requirement_text
    st.session_state.human_in_the_loop = human_in_the_loop
    # The starting data handed to the first pipeline step. A chosen knowledge
    # base is loaded; "create something new" (kb_name == "") loads nothing and
    # makes the run a from-scratch build.
    st.session_state.pipeline_state = {
        "run_id": run_id,
        "requirement_text": requirement_text,
        "applications": kb_loader.load_all(kb_loader.kb_path(kb_name)) if kb_name else [],
        "kb_name": kb_name,
        "force_from_scratch": not kb_name,
        "start_at": start_at,
        "iteration": 0,
        "history": [],
        "agent_models": agent_models,
    }
    st.session_state.current_node = "requirement"   # which step runs next
    st.session_state.completed_events = []          # finished steps, for redrawing
    st.session_state.awaiting_approval = False      # is a human decision pending?
    st.session_state.awaiting_jira = False          # is Jira permission pending?
    st.session_state.awaiting_kb = False            # is the "save knowledge base?" answer pending?
    st.session_state.awaiting_confluence = False    # is the "publish to Confluence?" answer pending?
    st.session_state.finished = False
    st.session_state.error = None


# If the form was just submitted (and no run is already going): validate
# the input, then start a new run.
if submitted and not st.session_state.get("run_active"):
    if not requirement_text.strip():
        st.warning("Enter a requirement.")
        st.stop()

    start_at = START_OPTIONS[start_choice]

    st.divider()
    _start_new_run(requirement_text, human_in_the_loop, agent_model_choices, selected_kb, start_at)
    if start_at != "requirement":
        st.caption(f"Starts with: **{start_choice}**.")
    st.caption(
        f"Knowledge base: **{selected_kb}**" if selected_kb
        else "Knowledge base: **none — creating something new** (you'll be asked at the end whether to save it)."
    )
    if agent_model_choices:
        overrides = ", ".join(
            f"{AGENT_DISPLAY_NAMES.get(k, k)} → `{v}`" for k, v in agent_model_choices.items()
        )
        st.caption(f"Model overrides for this run: {overrides}. Everything else: `{config.MODEL_ID}`.")


# ---- Saved sessions: carry on where an earlier session stopped ----
def _autosave() -> None:
    # Keep the current session's snapshot on disk, so it can be reopened later (see session_store.py).
    session_store.save(st.session_state)


def _resume_session(run_id: str) -> None:
    # Put a saved session back on the page. Runs as a button callback, before the page is redrawn.
    restored = session_store.restore(run_id, lambda name: kb_loader.load_all(kb_loader.kb_path(name)))
    if restored is None:
        st.session_state["_resume_error"] = f"Session {run_id} could not be opened (its file is missing or damaged)."
        return
    for key in [k for k in st.session_state.keys() if isinstance(k, str) and session_store.is_session_key(k)]:
        del st.session_state[key]                      # nothing of the session on screen may leak into the opened one
    st.session_state.update(restored["session"])
    st.session_state.pipeline_state = restored["pipeline_state"]
    st.session_state.completed_events = restored["completed_events"]
    for flag in ("awaiting_approval", "awaiting_jira", "awaiting_kb", "awaiting_confluence", "finished"):
        st.session_state.setdefault(flag, False)
    st.session_state.setdefault("error", None)
    st.session_state["_resume_error"] = None
    st.session_state["_resumed_run"] = run_id


def _ask_delete_session(run_id: str) -> None:
    st.session_state["_confirm_delete"] = run_id


def _delete_session(run_id: str) -> None:
    session_store.delete(run_id)
    st.session_state["_confirm_delete"] = None


def _cancel_delete_session() -> None:
    st.session_state["_confirm_delete"] = None


def _session_status(item: dict) -> str:
    # Where a saved session stands, in plain words.
    if item["error"]:
        return "Stopped with an error"
    if item["finished"]:
        return "Finished"
    where = NODE_DISPLAY.get(item["current_node"], item["current_node"] or "")
    waiting = item["waiting"]
    if "approval" in waiting:
        return "Waiting for your decision"
    if waiting:
        return "Waiting for your answer: " + {"jira": "Jira", "kb": "knowledge base", "confluence": "Confluence"}[waiting[0]]
    return f"In progress — continues at {where}" if where else "In progress"


_saved = [s for s in session_store.list_sessions() if s["run_id"] != st.session_state.get("run_id")]
if _saved:
    with st.expander(f"🕘 Saved sessions ({len(_saved)})", expanded=False):
        st.caption("Every session is saved as you go. Open one to carry on exactly where you left off — "
                   "what you have already done is not run again.")
        for item in _saved[:15]:
            left, open_col, delete_col = st.columns([5, 1, 1], vertical_alignment="center")
            left.markdown(f"**{item['title']}**  \n"
                          f"{_session_status(item)} · saved {item['saved_at'].replace('T', ' ')[:16]} UTC")
            open_col.button("Open", key=f"resume_{item['run_id']}", on_click=_resume_session, args=(item["run_id"],),
                            use_container_width=True)
            if st.session_state.get("_confirm_delete") == item["run_id"]:
                delete_col.button("Confirm", key=f"confirm_delete_{item['run_id']}", type="primary",
                                  on_click=_delete_session, args=(item["run_id"],), use_container_width=True)
                st.caption("Delete this saved session? Its documents stay; only the saved place is forgotten.")
                st.button("Keep it", key=f"keep_{item['run_id']}", on_click=_cancel_delete_session)
            else:
                delete_col.button("Delete", key=f"delete_{item['run_id']}", on_click=_ask_delete_session,
                                  args=(item["run_id"],), use_container_width=True)
if st.session_state.get("_resume_error"):
    st.error(st.session_state["_resume_error"])
if st.session_state.pop("_resumed_run", None) == st.session_state.get("run_id"):
    st.success("Session reopened. Carry on from where you left off.")

# Everything below draws the run's progress. It executes on every Streamlit
# re-run, so it first redraws what has already finished, then continues.
# The requirement editor (if it is on screen this run) sets these; start each run clean.
st.session_state["_pending_requirement_edit"] = None
st.session_state["_requirement_edit_invalid"] = False
st.session_state["_pending_design_edits"] = None
st.session_state["_design_edit_invalid"] = False
st.session_state["_pending_quality_edit"] = None
st.session_state["_quality_edit_invalid"] = False

if st.session_state.get("run_started"):
    st.divider()

    # Placeholders: the progress bar, the stage results area, and the sidebar.
    pipeline_strip = st.empty()
    stage_area = st.container()
    hive_panel = st.sidebar.empty()

    def _refresh_hive() -> None:
        # Redraw the sidebar with the latest events/blackboard.
        with hive_panel.container():
            render_hive(st.session_state.run_id)

    _refresh_hive()

    # Replay everything already completed in a PREVIOUS script
    # execution, so nothing already shown ever disappears across an
    # Approve/Suggest-changes rerun.
    with stage_area:
        # seen_counts numbers repeat visits to a stage ("pass 2" etc.).
        seen_counts = {}
        last_index = len(st.session_state.completed_events) - 1
        for i, (node_name, node_result) in enumerate(st.session_state.completed_events):
            seen_counts[node_name] = seen_counts.get(node_name, 0) + 1
            # The stage currently awaiting a decision is expanded by
            # default -- otherwise its actual output (the plan itself)
            # stays hidden behind a one-line collapsed label, and the
            # approval gate right below it (which IS always visible)
            # looks like the only thing the agent produced.
            is_pending = i == last_index and st.session_state.awaiting_approval
            # The requirement can be reworded only while nothing has been built from it: at its
            # pause, when it is the very last thing that ran.
            at_pause = i == last_index and st.session_state.awaiting_approval
            can_edit = at_pause and (
                (node_name == "requirement" and not node_result.get("requirement_skipped")
                 and not st.session_state.pipeline_state.get("design"))
                or (node_name == "design" and not st.session_state.pipeline_state.get("release_plan"))
                or (node_name == "quality_planning" and not st.session_state.pipeline_state.get("release_plan"))
            )
            with st.status(
                stage_label(node_name, node_result, seen_counts[node_name]),
                state=stage_state(node_name, node_result),
                expanded=is_pending,
            ):
                render_stage_body(node_name, node_result, editable=can_edit, edit_id=i + 1)

    # Update the progress bar to the most recently finished stage.
    if st.session_state.completed_events:
        with pipeline_strip.container():
            render_pipeline(st.session_state.completed_events[-1][0])

    # ---- THE MAIN LOOP ----
    # While a run is active and not waiting for a human, run the next
    # stage, show its result, and work out which stage comes after it.
    # The loop pauses (break) at an approval gate or when the run ends.
    if (
        st.session_state.run_active
        and not st.session_state.awaiting_approval
        and not st.session_state.get("awaiting_jira")
        and not st.session_state.get("awaiting_kb")
        and not st.session_state.get("awaiting_confluence")
    ):
        while True:
            current_node = st.session_state.current_node
            # No next stage -> the run is complete.
            if current_node is None:
                st.session_state.finished = True
                st.session_state.run_active = False
                break

            # About to create tickets in the REAL Jira: stop and ask the human
            # first (in both modes). Nothing is sent until they say yes.
            if (
                current_node == "jira_publish"
                and jira_client.is_configured()
                and st.session_state.pipeline_state.get("jira_approved") is None
            ):
                st.session_state.awaiting_jira = True
                break

            # Publishing the design to Confluence: ask first (and let the person reword it).
            design_now = st.session_state.pipeline_state.get("design") or {}
            if (
                current_node == "confluence_publish"
                and confluence_client.is_configured()
                and design_now.get("change_required") and design_now.get("components")
                and st.session_state.pipeline_state.get("confluence_approved") is None
            ):
                st.session_state.awaiting_confluence = True
                break

            # The knowledge-base question: offer to save the knowledge-base documents the
            # design proposed. Stop and ask first; nothing is written without a yes.
            if (
                current_node == "kb_save"
                and st.session_state.pipeline_state.get("kb_approved") is None
                and graph.kb_save_preview(st.session_state.pipeline_state)["mode"] != "none"
            ):
                st.session_state.awaiting_kb = True
                break

            running_label = NODE_DISPLAY.get(current_node, current_node)

            # Show a "running" box for this stage while it works.
            with stage_area:
                with st.status(running_label, state="running", expanded=False) as status_box:
                    try:
                        # Run the stage in the background with a live label.
                        result = _run_with_live_status(
                            status_box,
                            running_label,
                            NODE_FN[current_node],
                            st.session_state.pipeline_state,
                        )
                    except Exception as e:
                        # The stage failed: show an error and stop the run.
                        status_box.update(label="Pipeline error", state="error")
                        st.session_state.error = str(e)
                        st.session_state.run_active = False
                        break

                    # Merge this stage's output into the shared run data.
                    st.session_state.pipeline_state.update(result)

                    # A human note aimed at this stage has now been used up.
                    pending = st.session_state.pipeline_state.get("pending_human_note")
                    if pending and pending.get("stage") == current_node:
                        st.session_state.pipeline_state.pop("pending_human_note", None)

                    # Remember this stage as finished (so it can be redrawn
                    # on later re-runs), and count which pass it was.
                    st.session_state.completed_events.append((current_node, result))
                    pass_number = _pass_number(current_node, st.session_state.completed_events)

                    # Will we pause here for approval? If so, leave this
                    # stage's result expanded so the human can read it.
                    about_to_gate = _is_gated(current_node)
                    status_box.update(
                        label=stage_label(current_node, result, pass_number),
                        state=stage_state(current_node, result),
                        expanded=about_to_gate,
                    )
                    render_stage_body(
                        current_node, result,
                        editable=about_to_gate and (
                            (current_node == "requirement" and not result.get("requirement_skipped"))
                            or current_node in ("design", "quality_planning")
                        ),
                        edit_id=len(st.session_state.completed_events),
                    )

            # Refresh the progress bar and sidebar.
            with pipeline_strip.container():
                render_pipeline(current_node)
            _refresh_hive()

            # Human-approval mode: pause here and wait for the person.
            if _is_gated(current_node):
                st.session_state.awaiting_approval = True
                _autosave()
                break

            # Otherwise work out the next stage (using the real routing
            # rules from graph.py) and loop around.
            predict_fn = NEXT_NODE_FN.get(current_node)
            st.session_state.current_node = predict_fn(st.session_state.pipeline_state) if predict_fn else None
            _autosave()

    # Wherever the loop stopped (a pause, the end, an error), keep the session on disk.
    _autosave()

    # ------------------------------------------------
    # Approval gate.
    # Shown only in "Human approval" mode, after a stage finishes. The
    # person can: chat about it, answer the AI's questions, add notes,
    # approve, send suggestions back to the same stage, or redirect the
    # pipeline to a different stage ("override").
    # ------------------------------------------------
    if st.session_state.awaiting_approval:
        # The stage waiting for a decision and the result it produced.
        pending_name = st.session_state.current_node
        pending_result = (
            st.session_state.completed_events[-1][1] if st.session_state.completed_events else {}
        )
        pending_stage_output = pending_result.get(RESULT_KEY.get(pending_name, pending_name), {})
        # The AI's open questions for this stage (if any).
        questions = pending_stage_output.get("clarifying_questions", [])
        # A number unique to this gate, so each gate's widgets have their own state.
        gate_n = len(st.session_state.completed_events)
        no_preference = "No preference — proceed with the agent's recommendation"

        # Frozen: the document is shown above with its downloads; the person may also publish a frozen
        # design to Confluence, then continues to the next agent.
        if pending_name in FREEZE and stage_is_frozen(pending_name):
            frozen_key = f"{pending_name}_frozen_at"
            _, what, next_agent = FREEZE[pending_name]
            state_now = st.session_state.pipeline_state
            with st.container(border=True):
                st.markdown(f":material/lock: **{what} frozen** — download the document above if you want it"
                            + (", publish it to Confluence" if pending_name == "design" else "")
                            + f", then continue to the {next_agent} agent.")
                if pending_name == "design":
                    render_freeze_confluence(state_now, gate_n)
                go_col, back_col = st.columns(2)
                if back_col.button("Unfreeze & keep editing", key=f"unfreeze_{gate_n}", use_container_width=True):
                    st.session_state[frozen_key] = None
                    st.rerun()
                if go_col.button(f"Continue to {next_agent}", key=f"continue_{pending_name}_{gate_n}", type="primary",
                                 use_container_width=True):
                    st.session_state[frozen_key] = None
                    if pending_name == "design" and state_now.get("confluence_approved") is None:
                        # The choice was offered here, so the end of the run does not ask again.
                        state_now["confluence_approved"] = False
                    st.session_state.awaiting_approval = False
                    predict_fn = NEXT_NODE_FN.get(pending_name)
                    st.session_state.current_node = predict_fn(state_now) if predict_fn else None
                    st.rerun()
            st.stop()

        with st.container(border=True):
            # ---- Optional chat with the Discussion Assistant ----
            chat_key = f"chat_history_{gate_n}"
            if chat_key not in st.session_state:
                st.session_state[chat_key] = []

            with st.expander(
                "💬 Discuss before deciding", expanded=bool(st.session_state[chat_key])
            ):
                for msg in st.session_state[chat_key]:
                    with st.chat_message(msg["role"]):
                        st.write(msg["content"])

                # When the person sends a message: store it, ask the
                # assistant for a reply, store that, and redraw the page.
                user_msg = st.chat_input(
                    "Ask a question or share a thought...", key=f"chat_input_{gate_n}"
                )
                if user_msg:
                    st.session_state[chat_key].append({"role": "user", "content": user_msg})
                    with st.spinner("Thinking…"):
                        try:
                            reply_text = discuss_agent.reply(
                                st.session_state[chat_key],
                                st.session_state.pipeline_state,
                                pending_name,
                            )
                        except Exception as e:
                            reply_text = f"Couldn't get a response: {e}"
                    st.session_state[chat_key].append({"role": "assistant", "content": reply_text})
                    st.rerun()

            # ---- The AI's questions, as multiple-choice radio buttons ----
            if questions:
                st.markdown(
                    f":material/pause_circle: **Your call** — "
                    f"**{NODE_DISPLAY.get(pending_name, pending_name)}** has a "
                    f"decision it can't make for you. Weigh in below, add a note "
                    f"of your own, or approve to go with its recommendation."
                )
                answers = {}
                for i, q in enumerate(questions):
                    options = list(q.get("options", []))
                    choice = st.radio(
                        q.get("question", f"Question {i + 1}"),
                        options + [no_preference],
                        index=len(options),  # default: no answer forced on the user
                        key=f"cq_{gate_n}_{i}",
                        help=q.get("why_it_matters") or None,
                    )
                    # Only record real answers; "no preference" is skipped.
                    if choice != no_preference:
                        answers[q.get("question", f"Question {i + 1}")] = choice
            else:
                # No questions: just ask for a plain sign-off.
                st.markdown(
                    f":material/pause_circle: **Ready for your sign-off** — "
                    f"**{NODE_DISPLAY.get(pending_name, pending_name)}** found nothing "
                    f"ambiguous to flag. Approve to continue, or add a note below."
                )
                answers = {}

            # Free-text box for any extra suggestion.
            note = st.text_area(
                "Tell the Design agent what to change (optional)" if pending_name == "design" else "Chat",
                key=f"note_{gate_n}",
                height=100 if pending_name == "design" else 80,
                placeholder=("e.g. Put a message queue between the order service and the email service instead of direct "
                             "calls; add a cache in front of the catalogue." if pending_name == "design" else None),
                help=("The Design agent redoes the whole design following your instructions, and you review it again. "
                      "Any wording edits you made are replaced." if pending_name == "design" else None),
            )

            # The two main buttons: Approve, or Answer (send feedback back).
            gate_col1, gate_col2 = st.columns(2)
            # Approving is blocked while reworded requirement text is invalid (empty title,
            # scope or criteria), so a broken requirement can never reach the Design agent.
            edit_blocks_approval = (
                (pending_name == "requirement" and st.session_state.get("_requirement_edit_invalid"))
                or (pending_name == "design" and st.session_state.get("_design_edit_invalid"))
                or (pending_name == "quality_planning" and st.session_state.get("_quality_edit_invalid"))
            )
            if edit_blocks_approval:
                st.warning("Fix the problem in “Edit the test plan” above before freezing."
                           if pending_name == "quality_planning" else
                           "Fix the problem in “Edit the wording of …” above before approving."
                           if pending_name == "requirement" else
                           "Fix the problem in “Edit the wording of the design document” above before approving.")
            suggest_clicked = gate_col1.button(
                "Revise the design" if pending_name == "design" else "Reconsider",
                key=f"suggest_{gate_n}",
                use_container_width=True,
            )
            approve_clicked = gate_col2.button(
                FREEZE[pending_name][0] if pending_name in FREEZE else "Approve & continue",
                key=f"approve_{gate_n}",
                type="primary",
                use_container_width=True,
                disabled=bool(edit_blocks_approval),
            )

            # ---- Override: send the run to a different stage ----
            # Pre-select the stage that would normally come next.
            natural_next_fn = NEXT_NODE_FN.get(pending_name)
            natural_next = natural_next_fn(st.session_state.pipeline_state) if natural_next_fn else None
            default_target = natural_next if natural_next in OVERRIDE_TARGETS else pending_name
            default_index = (
                OVERRIDE_TARGETS.index(default_target) if default_target in OVERRIDE_TARGETS else 0
            )

            with st.expander("Override: send this to a different agent", expanded=False):
                st.caption(
                    "Redirect the pipeline instead of following its normal routing "
                    "-- skip ahead, send it back to an earlier stage, or re-run this "
                    "one with fresh direction. The target agent runs with whatever "
                    "state already exists; picking one whose inputs haven't been "
                    "produced yet (e.g. Engineering Manager before Design has ever run) "
                    "will error out the run."
                )
                override_target = st.selectbox(
                    "Next agent",
                    OVERRIDE_TARGETS,
                    index=default_index,
                    format_func=lambda k: NODE_DISPLAY.get(k, k),
                    key=f"override_target_{gate_n}",
                )
                override_instruction = st.text_area(
                    "Instructions for that agent",
                    key=f"override_instruction_{gate_n}",
                    height=80,
                    placeholder=(
                        "e.g. Skip the phased rollout -- the team wants this shipped "
                        "in a single stage, not staged over days."
                    ),
                )
                override_clicked = st.button(
                    "Override & continue",
                    key=f"override_{gate_n}",
                    use_container_width=True,
                )

        # ---- What happens when each button is clicked ----
        # APPROVE: stop waiting and move on to the normal next stage.
        if approve_clicked:
            # Save any rewording of the requirement first, so what the Design agent reads is
            # exactly what the person approved.
            if pending_name == "requirement" and st.session_state.get("_pending_requirement_edit"):
                _apply_requirement_edit(st.session_state["_pending_requirement_edit"])
            if pending_name == "design" and st.session_state.get("_pending_design_edits"):
                _apply_design_edits(st.session_state["_pending_design_edits"])
            if pending_name == "quality_planning" and st.session_state.get("_pending_quality_edit"):
                _apply_quality_edit(st.session_state["_pending_quality_edit"])
            if pending_name in FREEZE:
                # Freezing makes the document; the person then downloads it (and may publish it) and continues.
                st.session_state[f"{pending_name}_frozen_at"] = len(st.session_state.completed_events)
                st.rerun()
            st.session_state.awaiting_approval = False
            predict_fn = NEXT_NODE_FN.get(pending_name)
            st.session_state.current_node = (
                predict_fn(st.session_state.pipeline_state) if predict_fn else None
            )
            st.rerun()

        # ANSWER & CONTINUE: bundle the answers + extra note into one message
        # and send it back to the SAME stage to revise its output.
        if suggest_clicked:
            note_parts = [f"Q: {q}\nA: {a}" for q, a in answers.items()]
            if note.strip():
                note_parts.append(f"Additional suggestion: {note.strip()}")
            combined_note = "\n\n".join(note_parts)

            # Nothing was entered -> remind the person what to do.
            if not combined_note:
                st.warning(
                    ("Describe what the Design agent should change, or click Approve to continue as-is."
                     if pending_name == "design" else
                     "Answer at least one question or add a suggestion, or click Approve to continue as-is.")
                )
            else:
                st.session_state.awaiting_approval = False
                if pending_name == "design":
                    st.session_state.pipeline_state.update({"confluence_approved": None, "confluence_result": {}})
                st.session_state.pipeline_state["pending_human_note"] = {
                    "stage": pending_name,
                    "note": combined_note,
                }
                # current_node is left unchanged, so the next script
                # execution re-runs this same stage with the note.
                st.rerun()

        # OVERRIDE: jump to the chosen stage, giving it the typed instructions.
        if override_clicked:
            if not override_instruction.strip():
                st.warning(
                    "Enter instructions for the agent you're sending this to, or "
                    "use Approve/Answer instead if there's nothing to redirect."
                )
            else:
                st.session_state.awaiting_approval = False
                st.session_state.current_node = override_target
                st.session_state.pipeline_state["pending_human_note"] = {
                    "stage": override_target,
                    "note": override_instruction.strip(),
                }
                # Jumping to a stage other than pending_name's natural next
                # is exactly what makes this an override, not a revision --
                # _revision_feedback_for() in orchestrator/graph.py matches
                # pending_human_note purely by stage name, so whichever
                # stage runs next picks this up the same way a same-stage
                # revision would.
                st.rerun()

    # ------------------------------------------------
    # Jira permission gate.
    # Shown just before the real Jira would be touched, in both modes. The
    # person sees what could be created and where, and TICKS exactly which
    # tickets to create: the epic, each story, and each story's sub-tasks.
    # "Create" sends only the ticked ones; "Skip" keeps the backlog as local
    # files only. Nothing is sent before one of the buttons is pressed.
    # ------------------------------------------------
    if st.session_state.get("awaiting_jira"):
        plan = st.session_state.pipeline_state.get("release_plan", {})
        info = jira_client.preview(plan)
        gate_id = len(st.session_state.completed_events)

        # One checkbox per ticket. Their widget keys, so "select all / none" can set them.
        ck = lambda key: f"jira_sel_{gate_id}_{key}"
        all_checkbox_keys = [ck("epic")] + [
            ck(k) for story in info["items"] for k in [story["key"]] + [t["key"] for t in story["tasks"]]
        ]

        # Everything starts ticked. (Set once here rather than via value=True, because
        # "Select all / none" also changes these, and Streamlit objects to both.)
        for key in all_checkbox_keys:
            st.session_state.setdefault(key, True)

        def _set_all_tickets(value: bool) -> None:
            for key in all_checkbox_keys:
                st.session_state[key] = value

        with st.container(border=True):
            st.markdown(":material/lock: **Permission needed — choose which tickets to create in Jira**")
            st.write(
                f"**Jira:** {info['base_url']} · project **{info['project_key']}**  \n"
                f"**Created as:** {info['account']}"
            )
            pick_col1, pick_col2, _ = st.columns([1, 1, 4])
            pick_col1.button("Select all", key=f"jira_all_{gate_id}", on_click=_set_all_tickets, args=(True,))
            pick_col2.button("Select none", key=f"jira_none_{gate_id}", on_click=_set_all_tickets, args=(False,))

            # `edited` starts as a copy of the plan; the "Edit wording" boxes below
            # fill in whatever the person changes. Only WORDS can be changed here.
            edited = copy.deepcopy(plan)
            ek = lambda *parts: f"jira_edit_{gate_id}_" + "_".join(parts)

            # Epic, then each story with its sub-tasks (a sub-task needs its story).
            chosen = []
            if st.checkbox(f"Epic: {info['epic_title']}", key=ck("epic")):
                chosen.append("epic")
            with st.expander("✎ Edit epic wording", expanded=False):
                edited["epic"]["title"] = st.text_input(
                    "Epic title", value=plan["epic"]["title"], key=ek("epic", "title"))
                edited["epic"]["description"] = st.text_area(
                    "Epic description", value=plan["epic"].get("description", ""), height=120,
                    key=ek("epic", "description"))
            for story_index, story in enumerate(info["items"]):
                story_on = st.checkbox(
                    f"{story['title']}  ·  {story['component']}  ·  {story['priority']}",
                    key=ck(story["key"]),
                )
                if story_on:
                    chosen.append(story["key"])
                for task in story["tasks"]:
                    indent, box = st.columns([0.05, 0.95])
                    task_on = box.checkbox(
                        f"Sub-task: {task['title']}", key=ck(task["key"]), disabled=not story_on,
                    )
                    if story_on and task_on:
                        chosen.append(task["key"])

                # Reword this story and its sub-tasks (titles, descriptions, criteria, steps).
                old_story, new_story = plan["stories"][story_index], edited["stories"][story_index]
                with st.expander(f"✎ Edit wording — {old_story['title']}", expanded=False):
                    new_story["title"] = st.text_input(
                        "Story title", value=old_story["title"], key=ek(story["key"], "title"))
                    new_story["description"] = st.text_area(
                        "Story description", value=old_story.get("description", ""), height=140,
                        key=ek(story["key"], "description"))
                    new_story["acceptance_criteria"] = _lines(st.text_area(
                        "Acceptance criteria (one per line)",
                        value="\n".join(old_story.get("acceptance_criteria", [])), height=120,
                        key=ek(story["key"], "criteria")))
                    for number, (old_task, new_task) in enumerate(zip(old_story["tasks"], new_story["tasks"]), start=1):
                        st.markdown(f"**Sub-task {number}**")
                        new_task["title"] = st.text_input(
                            "Sub-task title", value=old_task["title"], key=ek(story["key"], f"T{number}", "title"))
                        new_task["description"] = st.text_area(
                            "Sub-task description", value=old_task.get("description", ""), height=90,
                            key=ek(story["key"], f"T{number}", "description"))
                        new_task["subtasks"] = _reword_steps(old_task.get("subtasks", []), st.text_area(
                            "Steps inside this sub-task (one per line)",
                            value="\n".join(x["title"] for x in old_task.get("subtasks", [])), height=100,
                            key=ek(story["key"], f"T{number}", "steps")))

            n_epic = int("epic" in chosen)
            n_stories = sum(1 for s_ in info["items"] if s_["key"] in chosen)
            n_subs = sum(1 for s_ in info["items"] for t_ in s_["tasks"] if t_["key"] in chosen)
            st.write(
                f"**Selected:** {n_epic} epic, {n_stories} of {info['stories']} stories, "
                f"{n_subs} of {info['sub_tasks']} sub-tasks."
            )
            if n_stories and not n_epic:
                st.caption("No epic is selected, so the stories will be created without one.")

            # Reworded text: how many edits, and a title can never be left empty.
            wording_edits = graph.plan_text_changes(plan, edited)
            title_problems = (
                (["the epic"] if not edited["epic"]["title"].strip() else [])
                + [f"“{o['title']}”" for o, n in zip(plan["stories"], edited["stories"]) if not n["title"].strip()]
                + [f"a sub-task of “{o['title']}”" for o, n in zip(plan["stories"], edited["stories"])
                   for t in n["tasks"] if not t["title"].strip()]
            )
            if title_problems:
                st.error("Every ticket needs a title — missing for: " + ", ".join(title_problems)
                         + ". Fix this to send your wording changes.")
            elif wording_edits:
                st.caption(f"✎ {len(wording_edits)} wording edit(s) will be saved to the tickets and the local files.")
            st.caption(
                "Nothing has been sent yet. These are real tickets: undoing this means deleting "
                "them in Jira. Unselected tickets stay in the local files only. Any wording "
                "changes you make above are saved to the local files either way. " + info["mapping"]
            )
            jira_col1, jira_col2 = st.columns(2)
            create_clicked = jira_col1.button(
                "Create selected tickets in Jira", key=f"jira_create_{gate_id}",
                type="primary", use_container_width=True, disabled=not chosen or bool(title_problems),
            )
            skip_clicked = jira_col2.button(
                "Skip — keep local files only", key=f"jira_skip_{gate_id}",
                use_container_width=True,
            )

        if create_clicked or skip_clicked:
            # Save any wording changes first (to the plan and the local files), so what
            # is sent to Jira - or kept locally - is exactly what the person approved.
            if wording_edits and not title_problems:
                try:
                    update = graph.apply_ticket_edits(st.session_state.pipeline_state, edited)
                except ValueError as exc:
                    st.error(str(exc))
                    st.stop()
                st.session_state.pipeline_state.update(update)
                st.session_state.completed_events = [
                    (n, {**r, "release_plan": edited}) if n == "release_planning"
                    else (n, {**r, "release_result": update["release_result"]}) if n == "release_apply" and "release_result" in update
                    else (n, r)
                    for n, r in st.session_state.completed_events
                ]
            st.session_state.pipeline_state["jira_approved"] = bool(create_clicked)
            st.session_state.pipeline_state["jira_selection"] = chosen if create_clicked else None
            st.session_state.awaiting_jira = False
            st.rerun()

    # ------------------------------------------------
    # Knowledge-base gate.
    # The last question of the release: save the knowledge-base documents the
    # design proposed? For a "create something new" run they would form a NEW
    # knowledge base (the person names it); otherwise they are added to the
    # knowledge base that was selected. "Don't save" writes nothing.
    # ------------------------------------------------
    if st.session_state.get("awaiting_kb"):
        kb_info = graph.kb_save_preview(st.session_state.pipeline_state)
        kb_gate_id = len(st.session_state.completed_events)
        name_error = ""
        new_name = ""

        with st.container(border=True):
            if kb_info["mode"] == "new":
                st.markdown(":material/library_add: **Save this as a new knowledge base?**")
                st.write(
                    "This run designed something new. Saving it creates a new knowledge base "
                    "from the documents below, which you can pick in future runs."
                )
            else:
                st.markdown(
                    f":material/library_add: **Add these to the knowledge base “{kb_info['kb_name']}”?**"
                )
                st.write("New applications are added as documents; existing ones get a dated section appended (nothing is rewritten).")

            for u in kb_info["updates"]:
                verb = "Create" if u["action"] == "create" else "Append to"
                with st.expander(f"{verb} `{u['application']}`", expanded=False):
                    if u.get("reason"):
                        st.caption(u["reason"])
                    st.code(u["content"], language="markdown")

            if kb_info["mode"] == "new":
                typed = st.text_input(
                    "Name for the new knowledge base",
                    value=kb_info["suggested_name"],
                    key=f"kb_name_{kb_gate_id}",
                )
                new_name = kb_loader.normalise_kb_name(typed)
                if not new_name:
                    name_error = "Enter a name (letters and numbers)."
                elif new_name in kb_info["existing_names"]:
                    name_error = f"A knowledge base called “{new_name}” already exists — choose a different name."
                elif new_name != typed:
                    st.caption(f"Will be saved as `{new_name}`.")
                if name_error:
                    st.error(name_error)

            kb_col1, kb_col2 = st.columns(2)
            kb_save_clicked = kb_col1.button(
                "Save knowledge base", key=f"kb_save_{kb_gate_id}", type="primary",
                use_container_width=True, disabled=bool(name_error),
            )
            kb_skip_clicked = kb_col2.button(
                "Don't save", key=f"kb_skip_{kb_gate_id}", use_container_width=True,
            )

        if kb_save_clicked or kb_skip_clicked:
            st.session_state.pipeline_state["kb_approved"] = bool(kb_save_clicked)
            st.session_state.pipeline_state["new_kb_name"] = new_name
            st.session_state.awaiting_kb = False
            st.rerun()

    # ------------------------------------------------
    # Confluence gate.
    # The last question: publish the design document to Confluence, with its diagrams? The
    # person can reword the document first. "Skip" publishes nothing (the document stays
    # downloadable). Wording changes are saved either way.
    # ------------------------------------------------
    if st.session_state.get("awaiting_confluence"):
        cf_state = st.session_state.pipeline_state
        cf_design, cf_req = cf_state["design"], cf_state.get("requirement") or {}
        cf_gate_id = len(st.session_state.completed_events)
        _, page_title = design_doc.effective(cf_design, cf_req, cf_state.get("design_doc_edits"))
        cf = confluence_client.preview(page_title, len(design_doc.attachment_names(cf_design)))

        with st.container(border=True):
            st.markdown(":material/description: **Publish the design to Confluence?**")
            st.write(
                f"**Confluence:** {cf['base_url']} · space **{cf['space_key']}**"
                + (f" · under page {cf['parent_page_id']}" if cf["parent_page_id"] else "")
                + f"  \n**Created as:** {cf['account']}"
            )
            st.write(f"**Page title:** {page_title}  ·  {cf['attachments']} diagram(s) will be attached")
            st.caption(
                "The document is shown above, under Design. Reword it here if you like; nothing is sent until "
                "you press Publish. If a page with this title already exists in the space, it is never overwritten."
            )
            render_design_wording_editor(cf_design, cf_req, cf_gate_id)
            cf_invalid = st.session_state.get("_design_edit_invalid")
            if cf_invalid:
                st.warning("Fix the problem in “Edit the wording of the design document” above to publish.")
            cf_col1, cf_col2 = st.columns(2)
            cf_publish = cf_col1.button("Publish to Confluence", key=f"cf_publish_{cf_gate_id}", type="primary",
                                        use_container_width=True, disabled=bool(cf_invalid))
            cf_skip = cf_col2.button("Skip — don't publish", key=f"cf_skip_{cf_gate_id}", use_container_width=True)

        if cf_publish or cf_skip:
            if st.session_state.get("_pending_design_edits") and not cf_invalid:
                _apply_design_edits(st.session_state["_pending_design_edits"])
            st.session_state.pipeline_state["confluence_approved"] = bool(cf_publish)
            st.session_state.awaiting_confluence = False
            st.rerun()

    # ------------------------------------------------
    # Final result, once finished or errored.
    # ------------------------------------------------
    if st.session_state.finished or st.session_state.error:
        st.divider()

        # Save the run's record to disk (history.json) and show a summary.
        final_state = st.session_state.pipeline_state
        run_dir = run_log.dump_run_state(
            st.session_state.run_id,
            st.session_state.requirement_text,
            final_state,
            error=st.session_state.error,
        )

        with st.container(border=True):
            if st.session_state.error:
                # Something went wrong: show the error message.
                st.markdown(":material/error: **Pipeline error**")
                st.write(st.session_state.error)
            else:
                # Success outcomes get a green tick, others a warning sign.
                outcome = final_state.get("run_outcome", "UNKNOWN")
                icon = (
                    ":material/check_circle:"
                    if outcome in {"RELEASED_TO_JIRA", "APPROVED_RELEASED", "NO_CHANGE_REQUIRED"}
                    else ":material/warning:"
                )
                st.markdown(f"{icon} **{_humanize_outcome(outcome)}**")
                st.caption(f"{final_state.get('iteration', 0)} design pass(es)")

                # Creating the Jira tickets failed (wrong project key, missing
                # permission...). Offer a retry; it resumes without duplicates.
                if (final_state.get("jira_result") or {}).get("status") == "failed":
                    if st.button("Retry creating the tickets in Jira", key="jira_retry"):
                        with st.spinner("Creating tickets in Jira…"):
                            update = graph.jira_publish_node({**final_state, "jira_approved": True})
                        final_state.update(update)
                        final_state["run_outcome"] = graph._derive_run_outcome(final_state)
                        # Refresh the saved stage results so the page shows the new outcome.
                        st.session_state.completed_events = [
                            (n, {**r, "jira_result": final_state["jira_result"]}) if n == "jira_publish"
                            else (n, {**r, "run_outcome": final_state["run_outcome"]}) if n == "finalize"
                            else (n, r)
                            for n, r in st.session_state.completed_events
                        ]
                        st.rerun()

            if not st.session_state.error and (final_state.get("confluence_result") or {}).get("status") == "failed":
                if st.button("Retry publishing to Confluence", key="confluence_retry"):
                    with st.spinner("Publishing to Confluence…"):
                        update = graph.confluence_publish_node({**final_state, "confluence_approved": True})
                    final_state.update(update)
                    st.session_state.completed_events = [
                        (n, {**r, "confluence_result": final_state["confluence_result"]}) if n == "confluence_publish" else (n, r)
                        for n, r in st.session_state.completed_events
                    ]
                    st.rerun()

            st.caption(f"Run artifacts: `{run_dir}`")
