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
import random
import threading
import time

import streamlit as st

import agents.discuss as discuss_agent
import config
import knowledge_base.loader as kb_loader
import orchestrator.graph as graph
import run_log
import tools.jira_client as jira_client
from hive import blackboard, events

# Browser-tab title, icon and use the full page width.
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
    "release": "Release Planning",
}

# The "Start with" / "Stop after" choices -> the stage they mean.
START_OPTIONS = {"Requirement agent": "requirement", "Design agent (skip the Requirement agent)": "design"}
STOP_OPTIONS = {"Requirement agent": "requirement", "Design agent": "design", "Release (Jira backlog)": "release"}
STAGE_ORDER = ["requirement", "design", "release"]

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

        # Which agents run. Start with the Requirement agent (normal), or skip it and
        # let the Design agent work from your text exactly as written; and stop after
        # the Requirement agent, the Design agent, or carry on through the release.
        scope_col1, scope_col2 = st.columns(2)
        start_choice = scope_col1.selectbox(
            "Start with", list(START_OPTIONS), index=0,
            help=(
                "The Requirement agent turns your request into a structured requirement. Choose the "
                "Design agent to skip it: your text is then used as the requirement exactly as written "
                "(useful when it is already precise)."
            ),
        )
        stop_choice = scope_col2.selectbox(
            "Stop after", list(STOP_OPTIONS), index=len(STOP_OPTIONS) - 1,
            help=(
                "Where the run ends. After the Requirement agent you get just the structured requirement. "
                "After the Design agent you get the design (and are still offered to save its knowledge-base "
                "documents) but no Jira backlog. 'Release' runs everything."
            ),
        )

        # Architectural changes made BETWEEN the Requirement and Design agents.
        architecture_direction = st.text_area(
            "Architectural direction for the Design agent (optional)",
            height=90,
            help=(
                "Your own decisions about HOW to design it, e.g. 'reuse the existing event bus; no new "
                "databases; keep the API backward compatible'. The Design agent treats this as binding. "
                "In Human approval mode you can also add or change it after seeing the requirement."
            ),
            placeholder="e.g. Use the existing event bus; no new databases.",
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
    ("release_apply", "Release", ":material/rocket_launch:"),
]

# Steps that share a slot on the progress bar with another step.
STAGE_ALIASES = {
    "release_planning": "release_apply",
    "jira_publish": "release_apply",
    "kb_save": "release_apply",
    "finalize": "release_apply",
}

# The key each gated node's own result dict is nested under -- release_planning
# is the one place this diverges from the node name itself.
RESULT_KEY = {
    "requirement": "requirement",
    "design": "design",
    "release_planning": "release_plan",
}

# Friendly names for each pipeline step, shown in status messages.
NODE_DISPLAY = {
    "requirement": "Requirement",
    "design": "Design",
    "release_planning": "Release Planning",
    "release_apply": "Saving files",
    "jira_publish": "Jira",
    "kb_save": "Knowledge base",
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
    "DESIGN_ONLY": "Stopped after the Design agent, as you chose",
    "REQUIREMENT_ONLY": "Stopped after the Requirement agent, as you chose",
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
    "release_planning": graph.release_planning_node,
    "release_apply": graph.release_apply_node,
    "jira_publish": graph.jira_publish_node,
    "kb_save": graph.kb_save_node,
    "finalize": graph.finalize_node,
}

# Stages worth pausing on in human-in-the-loop mode -- release_apply
# and finalize are excluded because they only execute a decision
# (release_planning's plan) that was already gated one step earlier.
GATED_NODE_NAMES = {"requirement", "design", "release_planning"}

# Same set as GATED_NODE_NAMES, but ordered -- used to populate the
# override gate's "send to" dropdown in a stable, pipeline-shaped order.
OVERRIDE_TARGETS = ["requirement", "design", "release_planning"]

# What each node hands off to next. Reuses the graph's own router
# functions as the source of truth so this can't drift out of sync
# with the real routing logic.
NEXT_NODE_FN = {
    "requirement": graph.requirement_router,
    "design": graph.design_router,
    "release_planning": lambda s: "release_apply",
    "release_apply": lambda s: "jira_publish",
    "jira_publish": lambda s: "kb_save",
    "kb_save": lambda s: "finalize",
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


# --- Architecture diagrams ---
# The diagrams are described in "DOT", a plain-text language for graphs
# (boxes and arrows) that Streamlit can draw.

def _dot_escape(text: str) -> str:
    # Make a name safe to place inside quotes in the diagram text.
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run_apps():
    # The applications of the knowledge base THIS run is using (empty when the
    # user chose "create something new").
    return st.session_state.get("pipeline_state", {}).get("applications", [])


def _existing_design_dot() -> str:
    """The architecture as it exists TODAY -- every known application
    and the call edges already documented in the knowledge base, via
    kb_loader.existing_dependency_edges(). Independent of any run's
    output; always the same picture until the KB itself changes."""
    # Diagram header and default box/arrow styling.
    lines = [
        "digraph existing {",
        '  rankdir="LR";',
        '  bgcolor="transparent";',
        '  node [shape=box, style=filled, fillcolor="#EEF1FB", color="#4F46E5", fontname="Helvetica"];',
        '  edge [color="#6B7280"];',
    ]
    # One box per application...
    apps = _run_apps()
    for app in apps:
        lines.append(f'  "{_dot_escape(app.name)}";')
    # ...and one arrow for each documented "A depends on B".
    for caller, callee in kb_loader.existing_dependency_edges(apps):
        lines.append(f'  "{_dot_escape(caller)}" -> "{_dot_escape(callee)}";')
    lines.append("}")
    return "\n".join(lines)


def _new_design_dot(arch: dict, apps=None) -> str:
    """The architecture AFTER this run's proposed change: every known
    application plus any new ones, with affected/new applications and
    newly proposed contracts highlighted against the same baseline
    edges existing_dependency_edges() produced, so the diff is visible
    in one picture rather than requiring a side-by-side diff."""
    affected_names = {a["name"] for a in arch.get("affected_applications", [])}
    new_apps = arch.get("new_applications_needed", [])
    new_names = {a["name"] for a in new_apps}

    # Colour key: orange = application being changed, green dashed = brand
    # new application, blue = untouched, red arrows = new agreements
    # (contracts) between services.
    lines = [
        "digraph new_design {",
        '  rankdir="LR";',
        '  bgcolor="transparent";',
        '  node [shape=box, style=filled, fontname="Helvetica"];',
    ]
    # Existing applications (orange if affected by the change).
    # `apps` is the existing applications to draw. A from-scratch build passes
    # an empty list so none of the existing system appears in the picture.
    apps = _run_apps() if apps is None else apps
    for app in apps:
        name = _dot_escape(app.name)
        if app.name in affected_names:
            lines.append(f'  "{name}" [fillcolor="#FDE9D9", color="#D97706", penwidth=2];')
        else:
            lines.append(f'  "{name}" [fillcolor="#EEF1FB", color="#4F46E5"];')
    # Brand-new applications the plan proposes.
    for a in new_apps:
        name = _dot_escape(a["name"])
        lines.append(
            f'  "{name}" [fillcolor="#DCFCE7", color="#16A34A", penwidth=2, style="filled,dashed"];'
        )

    # Existing arrows, drawn faintly in the background.
    for caller, callee in kb_loader.existing_dependency_edges(apps):
        lines.append(
            f'  "{_dot_escape(caller)}" -> "{_dot_escape(callee)}" [color="#D1D5DB"];'
        )
    # New contracts: a bold red arrow from the producer to each consumer.
    for contract in arch.get("cross_service_contracts", []):
        producer = contract.get("producer", "")
        label = _dot_escape(contract.get("name", ""))
        for consumer in contract.get("consumers", []):
            lines.append(
                f'  "{_dot_escape(producer)}" -> "{_dot_escape(consumer)}" '
                f'[color="#DC2626", penwidth=2, label="{label}", fontcolor="#DC2626"];'
            )

    lines.append("}")
    return "\n".join(lines)


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

    if node_name == "design":
        design = node_result.get("design", {})
        tag = "change required" if design.get("change_required") else "no change needed"
        return f"Design{suffix} — {tag}"

    if node_name == "release_planning":
        plan = node_result.get("release_plan", {})
        title = plan.get("epic", {}).get("title", "")
        n = len(plan.get("stories", []))
        head = f"Release Planning{suffix}"
        return f"{head} — {title} ({n} stories)" if title else head

    if node_name == "release_apply":
        return "Backlog saved locally"

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


def render_stage_body(node_name: str, node_result: dict) -> None:
    """Write one stage's detailed content. Must be called from inside
    an open st.status(...) block, which supplies the header/icon."""

    # Each section below shows one stage's result in a readable layout.
    #
    # REQUIREMENT: short scope summary, with full details tucked away.
    # ------------------------------------------------
    if node_name == "requirement":
        req = node_result.get("requirement", {})
        if node_result.get("requirement_skipped"):
            st.caption("The Requirement agent was skipped: your text is used as the requirement, exactly as written.")
        st.write(req.get("scope_summary", ""))
        with st.expander("Details", expanded=False):
            st.json(req)
        render_clarifying_questions(req)

    # DESIGN: affected apps (left), unaffected apps (right), build order,
    # test strategy, contracts, risks, knowledge-base changes, diagrams.
    # ------------------------------------------------
    elif node_name == "design":
        design = node_result.get("design", {})
        direction = (st.session_state.get("pipeline_state", {}).get("architecture_direction") or "").strip()
        if direction:
            st.caption(f"Following your architectural direction: {direction}")
        col1, col2 = st.columns(2)
        with col1:
            st.caption("Affected / new applications")
            affected = design.get("affected_applications", [])
            new_apps = design.get("new_applications_needed", [])
            if not affected and not new_apps:
                st.write("—")
            for a in affected:
                st.markdown(f"**{a.get('name', '')}**")
                st.caption(a.get("role_in_change", ""))
            for a in new_apps:
                st.markdown(f"**{a.get('name', '')}** :blue-badge[new]")
                st.caption(a.get("purpose", ""))
        with col2:
            st.caption("Considered, not affected")
            unaffected = design.get("unaffected_applications_considered", [])
            if not unaffected:
                st.write("—")
            for a in unaffected:
                st.markdown(f"**{a.get('name', '')}**")
                st.caption(a.get("reason", ""))

        if design.get("dependency_order"):
            st.write("**Dependency order:** " + " → ".join(design["dependency_order"]))
        if design.get("test_strategy"):
            st.write("**Test strategy:** " + design["test_strategy"])
        if design.get("cross_service_contracts"):
            with st.expander("Cross-service contracts", expanded=False):
                st.json(design["cross_service_contracts"])
        if design.get("architecture_risks"):
            with st.expander("Risks", expanded=False):
                for risk in design["architecture_risks"]:
                    st.write(risk)

        # Proposed knowledge-base changes (written when the release is published).
        kb_updates = design.get("knowledge_base_updates", [])
        if kb_updates:
            with st.expander(f"Knowledge base changes ({len(kb_updates)})", expanded=True):
                st.caption(
                    "Nothing is saved yet. At the end of the release you'll be asked whether to "
                    "save these — as a new knowledge base, or into the one you selected."
                )
                for u in kb_updates:
                    verb = "Create" if u.get("action") == "create" else "Append to"
                    st.markdown(f"**{verb}** `{u.get('application', '')}`")
                    if u.get("reason"):
                        st.caption(u["reason"])
                    st.code(u.get("content", ""), language="markdown")

        # Before / after diagrams. With no knowledge base there is no
        # "before", so only the new design is drawn.
        scratch = bool(st.session_state.get("pipeline_state", {}).get("from_scratch"))
        if scratch:
            st.caption("From-scratch build — the existing knowledge base was not used.")
            st.graphviz_chart(_new_design_dot(design, []))
        elif _run_apps():
            diagram_col1, diagram_col2 = st.columns(2)
            with diagram_col1:
                st.caption("Existing design")
                st.graphviz_chart(_existing_design_dot())
            with diagram_col2:
                st.caption("New design (orange = changed, green = new, red = new/changed contract)")
                st.graphviz_chart(_new_design_dot(design))
        else:
            st.caption("New design (green = new, red = contract)")
            st.graphviz_chart(_new_design_dot(design))

        render_clarifying_questions(design)

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
    start_at: str = "requirement", stop_after: str = "release", direction: str = "",
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
        "stop_after": stop_after,
        "architecture_direction": direction.strip(),
        "iteration": 0,
        "history": [],
        "agent_models": agent_models,
    }
    st.session_state.current_node = "requirement"   # which step runs next
    st.session_state.completed_events = []          # finished steps, for redrawing
    st.session_state.awaiting_approval = False      # is a human decision pending?
    st.session_state.awaiting_jira = False          # is Jira permission pending?
    st.session_state.awaiting_kb = False            # is the "save knowledge base?" answer pending?
    st.session_state.finished = False
    st.session_state.error = None


# If the form was just submitted (and no run is already going): validate
# the input, then start a new run.
if submitted and not st.session_state.get("run_active"):
    if not requirement_text.strip():
        st.warning("Enter a requirement.")
        st.stop()

    start_at, stop_after = START_OPTIONS[start_choice], STOP_OPTIONS[stop_choice]
    if STAGE_ORDER.index(stop_after) < STAGE_ORDER.index(start_at):
        st.warning(f"“Stop after: {stop_choice}” comes before “Start with: {start_choice}” — there would be nothing to run.")
        st.stop()

    st.divider()
    _start_new_run(requirement_text, human_in_the_loop, agent_model_choices, selected_kb,
                   start_at, stop_after, architecture_direction)
    if start_at != "requirement" or stop_after != "release":
        st.caption(f"Runs: **{start_choice}** → **{stop_choice}**.")
    st.caption(
        f"Knowledge base: **{selected_kb}**" if selected_kb
        else "Knowledge base: **none — creating something new** (you'll be asked at the end whether to save it)."
    )
    if agent_model_choices:
        overrides = ", ".join(
            f"{AGENT_DISPLAY_NAMES.get(k, k)} → `{v}`" for k, v in agent_model_choices.items()
        )
        st.caption(f"Model overrides for this run: {overrides}. Everything else: `{config.MODEL_ID}`.")


# Everything below draws the run's progress. It executes on every Streamlit
# re-run, so it first redraws what has already finished, then continues.
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
            with st.status(
                stage_label(node_name, node_result, seen_counts[node_name]),
                state=stage_state(node_name, node_result),
                expanded=is_pending,
            ):
                render_stage_body(node_name, node_result)

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

            # The very last step: offer to save the knowledge-base documents the
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
                    render_stage_body(current_node, result)

            # Refresh the progress bar and sidebar.
            with pipeline_strip.container():
                render_pipeline(current_node)
            _refresh_hive()

            # Human-approval mode: pause here and wait for the person.
            if _is_gated(current_node):
                st.session_state.awaiting_approval = True
                break

            # Otherwise work out the next stage (using the real routing
            # rules from graph.py) and loop around.
            predict_fn = NEXT_NODE_FN.get(current_node)
            st.session_state.current_node = predict_fn(st.session_state.pipeline_state) if predict_fn else None

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

        with st.container(border=True):
            # ---- Optional chat with the Discussion Assistant ----
            chat_key = f"chat_history_{gate_n}"
            if chat_key not in st.session_state:
                st.session_state[chat_key] = []

            with st.expander(
                "💬 Discuss before deciding", expanded=bool(st.session_state[chat_key])
            ):
                st.caption(
                    "Ask why it made a call, poke at a trade-off, or just think "
                    "out loud -- this is just a conversation and doesn't change "
                    "the plan. Use Answer or Override below once you've decided "
                    "what you actually want."
                )
                chat_box = st.container(height=320)
                with chat_box:
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

            # BETWEEN the Requirement and Design agents: the person may make
            # architectural changes here - direction the Design agent must follow.
            show_direction = (
                pending_name == "requirement"
                and st.session_state.pipeline_state.get("stop_after") != "requirement"
            )
            direction_value = ""
            if show_direction:
                direction_value = st.text_area(
                    "Architectural changes for the Design agent (optional)",
                    value=st.session_state.pipeline_state.get("architecture_direction", ""),
                    key=f"arch_direction_{gate_n}",
                    height=90,
                    help=(
                        "Your own decisions about HOW to design this, applied before the Design agent "
                        "runs: technology choices, services to reuse, things to avoid. It treats them as binding."
                    ),
                    placeholder="e.g. Use the existing event bus; no new databases.",
                )

            # Free-text box for any extra suggestion.
            note = st.text_area(
                "Additional notes (optional)",
                key=f"note_{gate_n}",
                height=80,
            )

            # The two main buttons: Approve, or Answer (send feedback back).
            gate_col1, gate_col2 = st.columns(2)
            approve_clicked = gate_col1.button(
                "Approve & continue",
                key=f"approve_{gate_n}",
                type="primary",
                use_container_width=True,
            )
            suggest_clicked = gate_col2.button(
                "Answer & continue",
                key=f"suggest_{gate_n}",
                use_container_width=True,
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
                    "produced yet (e.g. Release Planning before Design has ever run) "
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

        # Keep whatever architectural direction was typed, whichever button was pressed.
        if show_direction and (approve_clicked or suggest_clicked or override_clicked):
            st.session_state.pipeline_state["architecture_direction"] = direction_value.strip()

        # ---- What happens when each button is clicked ----
        # APPROVE: stop waiting and move on to the normal next stage.
        if approve_clicked:
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
                    "Answer at least one question or add a suggestion, or click "
                    "Approve to continue as-is."
                )
            else:
                st.session_state.awaiting_approval = False
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
                    if outcome in {"RELEASED_TO_JIRA", "APPROVED_RELEASED", "NO_CHANGE_REQUIRED",
                               "DESIGN_ONLY", "REQUIREMENT_ONLY"}
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

            st.caption(f"Run artifacts: `{run_dir}`")
