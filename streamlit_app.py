import random
import threading
import time

import streamlit as st

import agents.discuss as discuss_agent
import config
import knowledge_base.loader as kb_loader
import orchestrator.graph as graph
import run_log
from hive import blackboard, events

st.set_page_config(page_title="Blueprint · Claude SDLC", page_icon="📐", layout="wide")

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

st.markdown('<p class="app-eyebrow">Claude SDLC</p>', unsafe_allow_html=True)
st.title("Blueprint")
st.markdown(
    "Turn one requirement into a cross-repo implementation plan — "
    "architecture, test strategy, specs, and a ready-to-file Jira epic — "
    "reasoned end-to-end by Claude."
)

applications = kb_loader.load_all()

if not applications:
    st.error(f"No knowledge base found at `{config.KNOWLEDGE_BASE_DIR}`.")
    st.stop()

with st.expander(f"Known applications ({len(applications)})", expanded=False):
    st.caption(
        "Demo data — a fictional platform used to exercise this system, "
        "not a real architecture."
    )
    for app in applications:
        st.markdown(f"#### {app.name}")
        st.markdown(app.content)
        st.divider()


# Display label per agent key (config.AGENT_NAMES) -- used by the
# form's per-agent model pickers below.
AGENT_DISPLAY_NAMES = {
    "requirement": "Requirement",
    "architecture": "Architecture",
    "quality": "Quality",
    "implementation_spec": "Implementation Spec",
    "review": "Review",
    "release": "Release Planning",
}

# ============================================================
# INPUT FORM
# ============================================================

st.subheader("Start a new run")

with st.container(border=True):
    with st.form("run_form"):
        requirement_text = st.text_area(
            "Requirement",
            height=140,
            help="Plain language is fine -- naming specific applications is the next stage's job, not yours.",
            placeholder=(
                "Notify users by email and push when a new episode of a show "
                "they're watching becomes available, respecting their existing "
                "notification preferences."
            ),
        )

        mode = st.radio(
            "Mode",
            ["Agentic", "Human approval"],
            horizontal=True,
            captions=[
                "Runs start to finish, unattended.",
                "Pauses at every stage for your review and input.",
            ],
            help=(
                "Human approval pauses after Requirement, Architecture, Quality, "
                "Implementation Spec, Review, and Release Planning -- at each "
                "pause, the agent's own clarifying questions (with concrete "
                "options) are put to you directly, alongside open space for "
                "your own suggestions. Your answers go back to that same "
                "stage, which revises its own output before you're asked "
                "again."
            ),
        )
        human_in_the_loop = mode == "Human approval"

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

        submitted = st.form_submit_button(
            "Draft the blueprint", type="primary", use_container_width=True
        )


# ============================================================
# STAGE METADATA
# ============================================================

STAGE_META = [
    ("requirement", "Requirement", ":material/description:"),
    ("architecture", "Architecture", ":material/architecture:"),
    ("quality", "Quality", ":material/verified:"),
    ("implementation_spec", "Spec", ":material/design_services:"),
    ("review", "Review", ":material/rate_review:"),
    ("release_apply", "Release", ":material/rocket_launch:"),
]

STAGE_ALIASES = {"release_planning": "release_apply", "finalize": "release_apply"}

# The key each gated node's own result dict is nested under -- release_planning
# is the one place this diverges from the node name itself.
RESULT_KEY = {
    "requirement": "requirement",
    "architecture": "architecture",
    "quality": "quality",
    "implementation_spec": "implementation_spec",
    "review": "review",
    "release_planning": "release_plan",
}

NODE_DISPLAY = {
    "requirement": "Requirement",
    "architecture": "Architecture",
    "quality": "Quality",
    "implementation_spec": "Implementation Spec",
    "review": "Review",
    "release_planning": "Release Planning",
    "release_apply": "Publishing",
    "finalize": "Done",
}

# Human-readable labels for the backend's internal enum-style strings --
# shown to a non-engineering audience, so SCREAMING_SNAKE_CASE outcomes
# and statuses need a plain-English gloss rather than appearing verbatim.
REVIEW_STATUS_DISPLAY = {"PASS": "Passed", "CHANGES_REQUESTED": "Changes requested"}

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

OUTCOME_DISPLAY = {
    "APPROVED_RELEASED": "Approved & released to Jira",
    "NO_CHANGE_REQUIRED": "No architecture change required",
    "REVIEW_BLOCKED": "Blocked in review",
    "EXHAUSTED_ITERATIONS": "Stopped -- too many review cycles",
    "BREAKER_TRIPPED": "Stopped by circuit breaker -- review findings kept recurring",
    "INCOMPLETE": "Incomplete",
    "UNKNOWN": "Unknown",
}


def _humanize_outcome(value: str) -> str:
    return OUTCOME_DISPLAY.get(value, value.replace("_", " ").title())

NODE_FN = {
    "requirement": graph.requirement_node,
    "architecture": graph.architecture_node,
    "quality": graph.quality_node,
    "implementation_spec": graph.implementation_spec_node,
    "review": graph.review_node,
    "release_planning": graph.release_planning_node,
    "release_apply": graph.release_apply_node,
    "finalize": graph.finalize_node,
}

# Stages worth pausing on in human-in-the-loop mode -- release_apply
# and finalize are excluded because they only execute a decision
# (release_planning's plan) that was already gated one step earlier.
GATED_NODE_NAMES = {
    "requirement",
    "architecture",
    "quality",
    "implementation_spec",
    "review",
    "release_planning",
}

# Same set as GATED_NODE_NAMES, but ordered -- used to populate the
# override gate's "send to" dropdown in a stable, pipeline-shaped order.
OVERRIDE_TARGETS = [
    "requirement",
    "architecture",
    "quality",
    "implementation_spec",
    "review",
    "release_planning",
]

# What each node hands off to next. Reuses the graph's own router
# functions as the source of truth so this can't drift out of sync
# with the real routing logic.
NEXT_NODE_FN = {
    "requirement": lambda s: "architecture",
    "architecture": graph.architecture_router,
    "quality": lambda s: "implementation_spec",
    "implementation_spec": lambda s: "review",
    "review": graph.review_router,
    "release_planning": lambda s: "release_apply",
    "release_apply": lambda s: "finalize",
    "finalize": lambda s: None,
}


# ============================================================
# RENDER HELPERS
# ============================================================

def render_pipeline(completed_node: str = None) -> None:
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


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _existing_design_dot() -> str:
    """The architecture as it exists TODAY -- every known application
    and the call edges already documented in the knowledge base, via
    kb_loader.existing_dependency_edges(). Independent of any run's
    output; always the same picture until the KB itself changes."""
    lines = [
        "digraph existing {",
        '  rankdir="LR";',
        '  bgcolor="transparent";',
        '  node [shape=box, style=filled, fillcolor="#EEF1FB", color="#4F46E5", fontname="Helvetica"];',
        '  edge [color="#6B7280"];',
    ]
    for app in applications:
        lines.append(f'  "{_dot_escape(app.name)}";')
    for caller, callee in kb_loader.existing_dependency_edges(applications):
        lines.append(f'  "{_dot_escape(caller)}" -> "{_dot_escape(callee)}";')
    lines.append("}")
    return "\n".join(lines)


def _new_design_dot(arch: dict) -> str:
    """The architecture AFTER this run's proposed change: every known
    application plus any new ones, with affected/new applications and
    newly proposed contracts highlighted against the same baseline
    edges existing_dependency_edges() produced, so the diff is visible
    in one picture rather than requiring a side-by-side diff."""
    affected_names = {a["name"] for a in arch.get("affected_applications", [])}
    new_apps = arch.get("new_applications_needed", [])
    new_names = {a["name"] for a in new_apps}

    lines = [
        "digraph new_design {",
        '  rankdir="LR";',
        '  bgcolor="transparent";',
        '  node [shape=box, style=filled, fontname="Helvetica"];',
    ]
    for app in applications:
        name = _dot_escape(app.name)
        if app.name in affected_names:
            lines.append(f'  "{name}" [fillcolor="#FDE9D9", color="#D97706", penwidth=2];')
        else:
            lines.append(f'  "{name}" [fillcolor="#EEF1FB", color="#4F46E5"];')
    for a in new_apps:
        name = _dot_escape(a["name"])
        lines.append(
            f'  "{name}" [fillcolor="#DCFCE7", color="#16A34A", penwidth=2, style="filled,dashed"];'
        )

    for caller, callee in kb_loader.existing_dependency_edges(applications):
        lines.append(
            f'  "{_dot_escape(caller)}" -> "{_dot_escape(callee)}" [color="#D1D5DB"];'
        )
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


def render_findings(findings) -> None:
    for finding in findings:
        severity = finding.get("severity", "?")
        color = "red" if severity == "BLOCKING" else "orange"
        st.badge(f"{severity} · {finding.get('id', '')}", color=color)
        st.write(finding.get("finding", ""))
        evidence = finding.get("evidence", "")
        if evidence:
            st.caption(evidence)


def stage_label(node_name: str, node_result: dict, pass_number: int = 1) -> str:
    """The informative, final label for a completed stage -- used as
    the st.status() title once its result is known. pass_number counts
    how many times this stage has run in this session (automated
    Review retries and human-requested revisions both increment it)."""
    suffix = f" — pass {pass_number}" if pass_number > 1 else ""

    if node_name == "requirement":
        title = node_result.get("requirement", {}).get("title", "")
        return f"Requirement{suffix} — {title}" if title else f"Requirement{suffix}"

    if node_name == "architecture":
        arch = node_result.get("architecture", {})
        tag = "change required" if arch.get("change_required") else "no change needed"
        return f"Architecture{suffix} — {tag}"

    if node_name == "quality":
        return f"Quality{suffix}"

    if node_name == "implementation_spec":
        n = len(node_result.get("implementation_spec", {}).get("specs", []))
        return f"Implementation Spec{suffix} — {n} application(s)"

    if node_name == "review":
        review = node_result.get("review", {})
        findings = review.get("findings", [])
        blocking = sum(1 for f in findings if f.get("severity") == "BLOCKING")
        followup = len(findings) - blocking
        status = REVIEW_STATUS_DISPLAY.get(review.get("status"), review.get("status", "?"))
        return f"Review{suffix} — {status} ({blocking} blocking, {followup} follow-up)"

    if node_name == "release_planning":
        title = node_result.get("release_plan", {}).get("epic", {}).get("title", "")
        return f"Release Planning{suffix} — {title}" if title else f"Release Planning{suffix}"

    if node_name == "release_apply":
        return "Publishing"

    if node_name == "finalize":
        return f"Done — {_humanize_outcome(node_result.get('run_outcome', 'UNKNOWN'))}"

    return NODE_DISPLAY.get(node_name, node_name)


def stage_state(node_name: str, node_result: dict) -> str:
    """The st.status() state for a completed stage. 'error' is used
    for a stage whose *outcome* is negative (review sent the plan
    back) -- not just for a hard pipeline exception -- since that's
    the more informative signal to show at a glance."""
    if node_name == "review":
        return "complete" if node_result.get("review", {}).get("status") == "PASS" else "error"
    return "complete"


def render_stage_body(node_name: str, node_result: dict) -> None:
    """Write one stage's detailed content. Must be called from inside
    an open st.status(...) block, which supplies the header/icon."""

    # ------------------------------------------------
    if node_name == "requirement":
        req = node_result.get("requirement", {})
        st.write(req.get("scope_summary", ""))
        with st.expander("Details", expanded=False):
            st.json(req)
        render_clarifying_questions(req)

    # ------------------------------------------------
    elif node_name == "architecture":
        arch = node_result.get("architecture", {})
        col1, col2 = st.columns(2)
        with col1:
            st.caption("Affected applications")
            affected = arch.get("affected_applications", [])
            new_apps = arch.get("new_applications_needed", [])
            if not affected and not new_apps:
                st.write("—")
            for a in affected:
                st.markdown(f"**{a['name']}**")
                st.caption(a["role_in_change"])
            for a in new_apps:
                st.markdown(f"**{a['name']}** :blue-badge[new]")
                st.caption(a["purpose"])
        with col2:
            st.caption("Considered, not affected")
            unaffected = arch.get("unaffected_applications_considered", [])
            if not unaffected:
                st.write("—")
            for a in unaffected:
                st.markdown(f"**{a['name']}**")
                st.caption(a["reason"])

        if arch.get("dependency_order"):
            st.write("**Dependency order:** " + " → ".join(arch["dependency_order"]))
        if arch.get("cross_service_contracts"):
            with st.expander("Cross-service contracts", expanded=False):
                st.json(arch["cross_service_contracts"])
        if arch.get("architecture_risks"):
            with st.expander("Architecture risks", expanded=False):
                for risk in arch["architecture_risks"]:
                    st.write(risk)

        diagram_col1, diagram_col2 = st.columns(2)
        with diagram_col1:
            st.caption("Existing design")
            st.graphviz_chart(_existing_design_dot())
        with diagram_col2:
            st.caption("New design (orange = changed, green = new, red = new/changed contract)")
            st.graphviz_chart(_new_design_dot(arch))

        render_clarifying_questions(arch)

    # ------------------------------------------------
    elif node_name == "quality":
        quality = node_result.get("quality", {})
        st.write(quality.get("test_strategy_summary", ""))
        with st.expander(
            f"{len(quality.get('per_application_test_scenarios', []))} scenario(s), "
            f"{len(quality.get('contract_tests', []))} contract test(s)",
            expanded=False,
        ):
            st.json(quality)
        render_clarifying_questions(quality)

    # ------------------------------------------------
    elif node_name == "implementation_spec":
        spec = node_result.get("implementation_spec", {})
        for s in spec.get("specs", []):
            with st.expander(f"{s['application']} — {s['summary']}", expanded=False):
                st.write(s["detailed_spec"])
                if s.get("depends_on"):
                    st.caption(f"Depends on: {', '.join(s['depends_on'])}")
                col1, col2, col3 = st.columns(3)
                if s.get("api_changes"):
                    with col1:
                        st.markdown("**API changes**")
                        for c in s["api_changes"]:
                            st.write(f"- {c}")
                if s.get("event_changes"):
                    with col2:
                        st.markdown("**Event changes**")
                        for c in s["event_changes"]:
                            st.write(f"- {c}")
                if s.get("data_changes"):
                    with col3:
                        st.markdown("**Data changes**")
                        for c in s["data_changes"]:
                            st.write(f"- {c}")
        render_clarifying_questions(spec)

    # ------------------------------------------------
    elif node_name == "review":
        review = node_result.get("review", {})
        findings = review.get("findings", [])
        st.write(review.get("summary", ""))
        if findings:
            render_findings(findings)
        if review.get("status") == "CHANGES_REQUESTED":
            st.caption("Sent back to Architecture for revision.")
        render_clarifying_questions(review)

    # ------------------------------------------------
    elif node_name == "release_planning":
        plan = node_result.get("release_plan", {})
        st.write(plan.get("rollout_narrative", ""))

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

    # ------------------------------------------------
    elif node_name == "release_apply":
        result = node_result.get("release_result", {})
        st.write(f"Epic: **{result.get('epic_id')}**")
        for ticket in result.get("tickets", []):
            with st.container(border=True):
                st.markdown(f"**{ticket['ticket_id']}: {ticket.get('title', '')}**")

                badge_cols = st.columns(4)
                with badge_cols[0]:
                    st.badge("Story", icon=":material/bookmark:", color="green")
                with badge_cols[1]:
                    priority = ticket.get("priority", "")
                    st.badge(priority, color=PRIORITY_BADGE_COLOR.get(priority, "gray"))
                with badge_cols[2]:
                    st.badge(f"{ticket.get('story_points', '?')} pts", color="blue")
                with badge_cols[3]:
                    st.caption(ticket.get("application", ""))

                if ticket.get("labels"):
                    st.caption("Labels: " + ", ".join(ticket["labels"]))
                if ticket.get("description"):
                    st.write(ticket["description"])

                acceptance_criteria = ticket.get("acceptance_criteria", [])
                if acceptance_criteria:
                    with st.expander(f"Acceptance Criteria ({len(acceptance_criteria)})", expanded=False):
                        for i, criterion in enumerate(acceptance_criteria):
                            st.checkbox(
                                criterion,
                                value=False,
                                disabled=True,
                                key=f"ac_{ticket['ticket_id']}_{i}",
                            )

                blocked = (
                    f"Is blocked by: {', '.join(ticket['blocked_by'])}"
                    if ticket.get("blocked_by")
                    else "Not blocked"
                )
                st.caption(blocked)

    # ------------------------------------------------
    elif node_name == "finalize":
        st.write(f"Outcome: **{_humanize_outcome(node_result.get('run_outcome', 'UNKNOWN'))}**")


def _pass_number(node_name: str, events) -> int:
    return sum(1 for n, _ in events if n == node_name)


# ============================================================
# HIVE PANEL (sidebar): live event log, blackboard, circuit breaker
# ============================================================

BREAKER_BADGE = {"ok": ("Breaker: ok", "green"), "steer": ("Breaker: steer", "orange"),
                 "constrain": ("Breaker: constrain", "orange"), "stop": ("Breaker: stop", "red")}
EVENT_ICON = {"stage_started": "▶", "stage_done": "✓", "stage_failed": "✗",
              "human_note": "👤", "breaker": "⚡"}


def render_hive(run_id: str, breaker_state: dict) -> None:
    st.markdown("### Hive")
    label, color = BREAKER_BADGE.get((breaker_state or {}).get("level", "ok"), BREAKER_BADGE["ok"])
    st.badge(label, color=color)
    if (breaker_state or {}).get("reason"):
        st.caption(breaker_state["reason"])

    board = blackboard.read(run_id)
    for section, title in (("human_decisions", "Human decisions"), ("open_questions", "Open questions"),
                           ("risks", "Risks"), ("decisions", "Decisions")):
        active = [e for e in board[section] if e["status"] == "active"]
        with st.expander(f"{title} ({len(active)})", expanded=section == "human_decisions" and bool(active)):
            for e in active:
                st.markdown(f"- **{e['stage']}**: {e['text']}")
            if not active:
                st.caption("Nothing yet.")

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

WORKING_VERBS = [
    ("Spelunking", "the knowledge base"),
    ("Discombobulating", "the dependency order"),
    ("Percolating", "cross-service contracts"),
    ("Noodling on", "the requirement"),
    ("Wrangling", "affected applications"),
    ("Cross-examining", "architecture risks"),
    ("Untangling", "the rollout sequence"),
    ("Marinating", "test scenarios"),
    ("Sifting through", "the review findings"),
    ("Reticulating", "the release plan"),
    ("Puzzling over", "acceptance criteria"),
    ("Ruminating over", "the implementation specs"),
]


def _run_with_live_status(status_box, base_label: str, fn, *args, **kwargs):
    result_box = {}
    error_box = {}
    done = threading.Event()

    def _worker():
        try:
            result_box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread below
            error_box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    verbs = random.sample(WORKING_VERBS, len(WORKING_VERBS))
    start = time.monotonic()
    i = 0
    while not done.wait(timeout=1.6):
        elapsed = int(time.monotonic() - start)
        verb, subject = verbs[i % len(verbs)]
        status_box.update(label=f"{base_label} — {verb} {subject}… ({elapsed}s)")
        i += 1

    thread.join()
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

def _start_new_run(requirement_text: str, human_in_the_loop: bool, agent_models: dict) -> None:
    run_id = graph.new_run_id()
    st.session_state.run_started = True
    st.session_state.run_active = True
    st.session_state.run_id = run_id
    st.session_state.requirement_text = requirement_text
    st.session_state.human_in_the_loop = human_in_the_loop
    st.session_state.pipeline_state = {
        "run_id": run_id,
        "requirement_text": requirement_text,
        "applications": applications,
        "iteration": 0,
        "history": [],
        "agent_models": agent_models,
    }
    st.session_state.current_node = "requirement"
    st.session_state.completed_events = []
    st.session_state.awaiting_approval = False
    st.session_state.finished = False
    st.session_state.error = None


if submitted and not st.session_state.get("run_active"):
    if not requirement_text.strip():
        st.warning("Enter a requirement.")
        st.stop()

    st.divider()
    _start_new_run(requirement_text, human_in_the_loop, agent_model_choices)
    if agent_model_choices:
        overrides = ", ".join(
            f"{AGENT_DISPLAY_NAMES.get(k, k)} → `{v}`" for k, v in agent_model_choices.items()
        )
        st.caption(f"Model overrides for this run: {overrides}. Everything else: `{config.MODEL_ID}`.")


if st.session_state.get("run_started"):
    st.divider()

    pipeline_strip = st.empty()
    stage_area = st.container()
    hive_panel = st.sidebar.empty()

    def _refresh_hive() -> None:
        with hive_panel.container():
            render_hive(
                st.session_state.run_id,
                st.session_state.pipeline_state.get("breaker", {}),
            )

    _refresh_hive()

    # Replay everything already completed in a PREVIOUS script
    # execution, so nothing already shown ever disappears across an
    # Approve/Suggest-changes rerun.
    with stage_area:
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

    if st.session_state.completed_events:
        with pipeline_strip.container():
            render_pipeline(st.session_state.completed_events[-1][0])

    if st.session_state.run_active and not st.session_state.awaiting_approval:
        while True:
            current_node = st.session_state.current_node
            if current_node is None:
                st.session_state.finished = True
                st.session_state.run_active = False
                break

            running_label = NODE_DISPLAY.get(current_node, current_node)

            with stage_area:
                with st.status(running_label, state="running", expanded=False) as status_box:
                    try:
                        result = _run_with_live_status(
                            status_box,
                            running_label,
                            NODE_FN[current_node],
                            st.session_state.pipeline_state,
                        )
                    except Exception as e:
                        status_box.update(label="Pipeline error", state="error")
                        st.session_state.error = str(e)
                        st.session_state.run_active = False
                        break

                    st.session_state.pipeline_state.update(result)

                    pending = st.session_state.pipeline_state.get("pending_human_note")
                    if pending and pending.get("stage") == current_node:
                        st.session_state.pipeline_state.pop("pending_human_note", None)

                    st.session_state.completed_events.append((current_node, result))
                    pass_number = _pass_number(current_node, st.session_state.completed_events)

                    about_to_gate = (
                        st.session_state.human_in_the_loop and current_node in GATED_NODE_NAMES
                    )
                    status_box.update(
                        label=stage_label(current_node, result, pass_number),
                        state=stage_state(current_node, result),
                        expanded=about_to_gate,
                    )
                    render_stage_body(current_node, result)

            with pipeline_strip.container():
                render_pipeline(current_node)
            _refresh_hive()

            if st.session_state.human_in_the_loop and current_node in GATED_NODE_NAMES:
                st.session_state.awaiting_approval = True
                break

            predict_fn = NEXT_NODE_FN.get(current_node)
            st.session_state.current_node = predict_fn(st.session_state.pipeline_state) if predict_fn else None

    # ------------------------------------------------
    # Approval gate.
    # ------------------------------------------------
    if st.session_state.awaiting_approval:
        pending_name = st.session_state.current_node
        pending_result = (
            st.session_state.completed_events[-1][1] if st.session_state.completed_events else {}
        )
        pending_stage_output = pending_result.get(RESULT_KEY.get(pending_name, pending_name), {})
        questions = pending_stage_output.get("clarifying_questions", [])
        gate_n = len(st.session_state.completed_events)
        no_preference = "No preference — proceed with the agent's recommendation"

        with st.container(border=True):
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
                    if choice != no_preference:
                        answers[q.get("question", f"Question {i + 1}")] = choice
            else:
                st.markdown(
                    f":material/pause_circle: **Ready for your sign-off** — "
                    f"**{NODE_DISPLAY.get(pending_name, pending_name)}** found nothing "
                    f"ambiguous to flag. Approve to continue, or add a note below."
                )
                answers = {}

            note = st.text_area(
                "Additional notes (optional)",
                key=f"note_{gate_n}",
                height=80,
            )

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
                    "produced yet (e.g. Release Planning before Review has ever run) "
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

        if approve_clicked:
            st.session_state.awaiting_approval = False
            predict_fn = NEXT_NODE_FN.get(pending_name)
            st.session_state.current_node = (
                predict_fn(st.session_state.pipeline_state) if predict_fn else None
            )
            st.rerun()

        if suggest_clicked:
            note_parts = [f"Q: {q}\nA: {a}" for q, a in answers.items()]
            if note.strip():
                note_parts.append(f"Additional suggestion: {note.strip()}")
            combined_note = "\n\n".join(note_parts)

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
    # Final result, once finished or errored.
    # ------------------------------------------------
    if st.session_state.finished or st.session_state.error:
        st.divider()

        final_state = st.session_state.pipeline_state
        run_dir = run_log.dump_run_state(
            st.session_state.run_id,
            st.session_state.requirement_text,
            final_state,
            error=st.session_state.error,
        )

        with st.container(border=True):
            if st.session_state.error:
                st.markdown(":material/error: **Pipeline error**")
                st.write(st.session_state.error)
            else:
                outcome = final_state.get("run_outcome", "UNKNOWN")
                icon = (
                    ":material/check_circle:"
                    if outcome in {"APPROVED_RELEASED", "NO_CHANGE_REQUIRED"}
                    else ":material/warning:"
                )
                st.markdown(f"{icon} **{_humanize_outcome(outcome)}**")
                st.caption(f"{final_state.get('iteration', 0)} iteration(s)")

            st.caption(f"Run artifacts: `{run_dir}`")
