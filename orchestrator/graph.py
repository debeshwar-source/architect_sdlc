"""
Architecture-level SDLC pipeline, wired as a LangGraph state machine.

    requirement -> architecture -> [change required?]
                                       |-> quality -> implementation_spec -> review
                                       '-> review (no-change verification: sanity-check the claim)
                   review -> [PASS?]
                                |-> release_planning -> release_apply -> finalize
                                '-> (retry) architecture, bounded by MAX_REVIEW_ITERATIONS

release_planning also produces the staged rollout plan (there was a
separate Testing stage for this; it added no independent reasoning --
it only reformatted architecture's dependency_order -- so it was
folded into release_planning to cut a redundant LLM call/stage).

Deliverable is the plan + Jira epic/tickets -- this orchestrator does
not invoke the granular ai_native_sdlc engine to write code. Each
implementation spec it produces is written to be usable as-is as that
engine's --spec input, for whoever picks up the resulting ticket.
"""

import functools
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

import agents.architecture as architecture_agent
import agents.implementation_spec as implementation_spec_agent
import agents.quality as quality_agent
import agents.release as release_agent
import agents.requirement as requirement_agent
import agents.review as review_agent
import config
import tools.issue_tracker as issue_tracker
from hive import blackboard, breaker, events, memory
from knowledge_base.loader import ApplicationDoc

MAX_REVIEW_ITERATIONS = config.MAX_REVIEW_ITERATIONS


class ArchitectureSDLCState(TypedDict, total=False):
    run_id: str
    requirement_text: str
    applications: List[ApplicationDoc]

    requirement: dict
    architecture: dict
    change_required: bool
    quality: dict
    implementation_spec: dict
    review: dict
    release_plan: dict
    release_result: dict

    # Set by the Streamlit UI's human-in-the-loop mode when a reviewer
    # asks a stage to revise its own last output, e.g.
    # {"stage": "architecture", "note": "..."}. A node consumes this
    # via _revision_feedback_for only when it matches that node's own
    # stage name; the CLI (no HITL) never sets this, so it's always
    # None there and every node behaves exactly as it did before this
    # field existed.
    pending_human_note: dict

    iteration: int
    run_outcome: str
    history: List[dict]

    # Per-agent Claude model override for this run, keyed by
    # config.AGENT_NAMES (e.g. {"architecture": "claude-opus-5-5"}) --
    # set once at run start (Streamlit's form / cli.py's --agent-model)
    # and read uniformly via _model_for. An agent missing from this
    # dict (or an empty/absent dict entirely) falls back to
    # config.MODEL_ID, so a run that never sets this behaves exactly
    # as it did before this field existed.
    agent_models: Dict[str, str]

    # Circuit-breaker state (hive/breaker.py), updated by review_node
    # after each CHANGES_REQUESTED verdict: {"level": ok|steer|
    # constrain|stop, "reason", "recurring", "records"}.
    breaker: dict


def append_history(state: ArchitectureSDLCState, entry: dict) -> List[dict]:
    history = list(state.get("history", []))
    history.append(entry)
    return history


def _model_for(state: ArchitectureSDLCState, agent_key: str) -> str:
    return (state.get("agent_models") or {}).get(agent_key) or config.MODEL_ID


def new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:6]}"


def _can_retry(state: ArchitectureSDLCState) -> bool:
    return state.get("iteration", 0) < MAX_REVIEW_ITERATIONS


# ============================================================
# HIVE INSTRUMENTATION
#
# Every node is wrapped by _hive(stage): it logs stage start/finish to
# the run's event log, mirrors the stage's output onto the blackboard,
# and turns a consumed human note into a standing human decision. This
# lives here (not in the CLI/Streamlit) so both entry points get it.
# ============================================================

_RESULT_KEY = {"release_planning": "release_plan"}


def _context_for(stage: str, state: ArchitectureSDLCState) -> str:
    """Hive background for a stage's prompt: standing human decisions
    for that stage, plus (for the two framing stages) recalled similar
    past runs. Empty string when there is nothing to add."""
    notes = blackboard.format_for_prompt(state["run_id"], stage)
    if stage in ("requirement", "architecture"):
        notes += memory.format_for_prompt(state["requirement_text"], state["run_id"])
    return notes


def _summarize(stage: str, result: dict) -> str:
    out = result.get(_RESULT_KEY.get(stage, stage)) or {}
    if stage == "requirement":
        return out.get("title", "")
    if stage == "architecture":
        names = [a.get("name", "") for a in out.get("affected_applications", [])]
        return f"change_required={out.get('change_required')}; affected: {', '.join(names) or 'none'}"
    if stage == "quality":
        return f"{len(out.get('per_application_test_scenarios', []))} scenario group(s)"
    if stage == "implementation_spec":
        return f"{len(out.get('specs', []))} spec(s)"
    if stage == "review":
        findings = out.get("findings", [])
        blocking = sum(1 for f in findings if f.get("severity") == "BLOCKING")
        return f"{out.get('status')} ({blocking} blocking, {len(findings) - blocking} follow-up)"
    if stage == "release_planning":
        return f"{len(out.get('tickets', []))} ticket(s)"
    if stage == "finalize":
        return result.get("run_outcome", "")
    return ""


def _post_stage_output(run_id: str, stage: str, result: dict, iteration: int) -> None:
    out = result.get(_RESULT_KEY.get(stage, stage))
    if not isinstance(out, dict):
        return
    blackboard.supersede(run_id, stage)

    if stage == "requirement":
        blackboard.post(run_id, "decisions", stage, out.get("scope_summary"), iteration)
    elif stage == "architecture":
        names = [a.get("name", "") for a in out.get("affected_applications", [])]
        blackboard.post(
            run_id, "decisions", stage,
            f"change_required={out.get('change_required')}; affected: {', '.join(names) or 'none'}; "
            f"order: {' -> '.join(map(str, out.get('dependency_order', [])))}",
            iteration,
        )
        for risk in out.get("architecture_risks", []):
            blackboard.post(
                run_id, "risks", stage,
                risk if isinstance(risk, str) else json.dumps(risk, default=str), iteration,
            )
    elif stage == "review":
        for f in out.get("findings", []):
            blackboard.post(
                run_id, "risks", stage, f.get("finding"), iteration, severity=f.get("severity")
            )

    for q in out.get("clarifying_questions", []):
        blackboard.post(run_id, "open_questions", stage, q.get("question"), iteration)


def _hive(stage: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(state: ArchitectureSDLCState):
            run_id = state["run_id"]
            pending = state.get("pending_human_note")
            human_note = pending["note"] if pending and pending.get("stage") == stage else None

            events.emit(run_id, "stage_started", stage, iteration=state.get("iteration", 0))
            try:
                result = fn(state)
            except Exception as exc:
                events.emit(run_id, "stage_failed", stage, error=str(exc))
                raise

            iteration = result.get("iteration", state.get("iteration", 0))
            if human_note:
                blackboard.resolve_questions(run_id, stage)
                blackboard.post(run_id, "human_decisions", stage, human_note, iteration)
                events.emit(run_id, "human_note", stage, note=human_note)

            _post_stage_output(run_id, stage, result, iteration)
            events.emit(run_id, "stage_done", stage, iteration=iteration, summary=_summarize(stage, result))

            new_breaker = result.get("breaker")
            if new_breaker and new_breaker.get("level") != breaker.OK:
                events.emit(
                    run_id, "breaker", stage,
                    level=new_breaker["level"], reason=new_breaker["reason"],
                )
            return result

        return wrapper

    return decorator


# ============================================================
# NODES
# ============================================================

@_hive("requirement")
def requirement_node(state: ArchitectureSDLCState):
    revision_feedback = _revision_feedback_for("requirement", state)
    requirement = requirement_agent.run(
        state["requirement_text"], state["applications"], revision_feedback,
        model=_model_for(state, "requirement"),
        context_notes=_context_for("requirement", state),
    )
    return {
        "requirement": requirement,
        "history": append_history(
            state, {"stage": "requirement", "status": "DONE", "result": requirement}
        ),
    }


@_hive("architecture")
def architecture_node(state: ArchitectureSDLCState):
    # A human revising architecture in place (via the HITL "suggest
    # changes" gate) must NOT consume MAX_REVIEW_ITERATIONS -- that
    # budget bounds the automated Review-to-Architecture retry loop, a
    # different, unsupervised risk. Only advance it on a genuine pass
    # through the graph.
    pending = state.get("pending_human_note")
    is_human_revision = bool(pending and pending.get("stage") == "architecture")
    iteration = state.get("iteration", 0) if is_human_revision else state.get("iteration", 0) + 1

    revision_feedback = _revision_feedback_for("architecture", state)

    architecture = architecture_agent.run(
        state["requirement"], state["applications"], revision_feedback,
        model=_model_for(state, "architecture"),
        context_notes=_context_for("architecture", state),
    )

    return {
        "architecture": architecture,
        "change_required": architecture["change_required"],
        "iteration": iteration,
        # A fresh architecture plan supersedes every downstream
        # artifact from a prior iteration -- without clearing these,
        # a revised architecture (e.g. one that now decides no change
        # is needed) would leave stale quality/spec output from a
        # previous pass sitting in state for review to see.
        "quality": {},
        "implementation_spec": {},
        "review": {},
        "history": append_history(
            state,
            {
                "stage": "architecture",
                "status": "DONE",
                "iteration": iteration,
                "result": architecture,
            },
        ),
    }


@_hive("quality")
def quality_node(state: ArchitectureSDLCState):
    revision_feedback = _revision_feedback_for("quality", state)
    quality = quality_agent.run(
        state["requirement"], state["architecture"], revision_feedback,
        model=_model_for(state, "quality"),
        context_notes=_context_for("quality", state),
    )
    return {
        "quality": quality,
        "history": append_history(
            state, {"stage": "quality", "status": "DONE", "result": quality}
        ),
    }


@_hive("implementation_spec")
def implementation_spec_node(state: ArchitectureSDLCState):
    revision_feedback = _revision_feedback_for("implementation_spec", state)
    spec = implementation_spec_agent.run(
        state["requirement"], state["architecture"], state["quality"], revision_feedback,
        model=_model_for(state, "implementation_spec"),
        context_notes=_context_for("implementation_spec", state),
    )
    return {
        "implementation_spec": spec,
        "history": append_history(
            state, {"stage": "implementation_spec", "status": "DONE", "result": spec}
        ),
    }


@_hive("review")
def review_node(state: ArchitectureSDLCState):
    revision_feedback = _revision_feedback_for("review", state)
    review = review_agent.run(
        state["requirement"],
        state["architecture"],
        state.get("quality", {}),
        state.get("implementation_spec", {}),
        state["applications"],
        revision_feedback,
        model=_model_for(state, "review"),
        context_notes=_context_for("review", state),
    )
    update = {
        "review": review,
        "history": append_history(
            state, {"stage": "review", "status": review["status"], "result": review}
        ),
    }
    if review["status"] == "CHANGES_REQUESTED":
        update["breaker"] = breaker.evaluate(
            state.get("breaker"),
            state.get("iteration", 0),
            review.get("findings", []),
            len((state.get("architecture") or {}).get("affected_applications", [])),
        )
    else:
        # A passing review ends the retry loop; clear any stale trip.
        update["breaker"] = {"level": breaker.OK, "reason": "", "recurring": 0, "records": []}
    return update


@_hive("release_planning")
def release_planning_node(state: ArchitectureSDLCState):
    revision_feedback = _revision_feedback_for("release_planning", state)
    release_plan = release_agent.run(
        state["requirement"], state["architecture"], state["review"], revision_feedback,
        model=_model_for(state, "release"),
        context_notes=_context_for("release_planning", state),
    )
    return {
        "release_plan": release_plan,
        "history": append_history(
            state, {"stage": "release_planning", "status": "DONE", "result": release_plan}
        ),
    }


def release_apply_node(state: ArchitectureSDLCState):
    run_dir = config.RUNS_ROOT / state["run_id"]
    release_result = issue_tracker.write_release(
        run_dir / "jira_mock",
        state["release_plan"]["epic"],
        state["release_plan"]["tickets"],
        state["run_id"],
    )
    return {
        "release_result": release_result,
        "history": append_history(
            state, {"stage": "release_apply", "status": "PASS", "result": release_result}
        ),
    }


@_hive("finalize")
def finalize_node(state: ArchitectureSDLCState):
    outcome = _derive_run_outcome(state)
    history = append_history(
        state, {"stage": "finalize", "status": outcome, "result": {"run_outcome": outcome}}
    )
    memory.remember({**state, "run_outcome": outcome, "history": history})
    return {"run_outcome": outcome, "history": history}


# Stages the AUTOMATED Review-to-Architecture retry loop re-runs on
# its own, in bounded response to Review's own BLOCKING findings. A
# human's ad hoc "suggest changes" note (below) applies to any stage;
# this narrower, iteration-gated fallback must not, since it was only
# ever designed to fire on that specific automated retry pass -- e.g.
# review_node runs again on every such pass too, but as the producer
# of those findings, not their target, so it must never be handed its
# own prior findings as if they were feedback about itself.
_AUTO_RETRY_STAGES = {"architecture", "quality", "implementation_spec"}


def _revision_feedback_for(stage: str, state: ArchitectureSDLCState) -> dict:
    pending = state.get("pending_human_note")
    if pending and pending.get("stage") == stage:
        return {
            "stage_being_revised": stage,
            "review_findings": [
                {
                    "id": "human-note",
                    "severity": "BLOCKING",
                    "finding": pending["note"],
                    "evidence": "Note from a human reviewer",
                    "suggested_fix": "",
                }
            ],
        }

    if stage not in _AUTO_RETRY_STAGES:
        return None
    if state.get("iteration", 0) <= 1:
        return None
    review = state.get("review") or {}
    if review.get("status") != "CHANGES_REQUESTED":
        return None
    feedback = {"stage_being_revised": stage, "review_findings": review.get("findings", [])}
    directive = breaker.directive_for(state.get("breaker"))
    if directive:
        feedback["circuit_breaker"] = directive
    return feedback


def _derive_run_outcome(state: ArchitectureSDLCState) -> str:
    if state.get("release_result"):
        return "APPROVED_RELEASED"

    if not state.get("change_required") and state.get("review", {}).get("status") == "PASS":
        return "NO_CHANGE_REQUIRED"

    if state.get("review", {}).get("status") == "CHANGES_REQUESTED":
        if (state.get("breaker") or {}).get("level") == breaker.STOP:
            return "BREAKER_TRIPPED"
        return "REVIEW_BLOCKED"

    if state.get("iteration", 0) >= MAX_REVIEW_ITERATIONS:
        return "EXHAUSTED_ITERATIONS"

    return "INCOMPLETE"


# ============================================================
# ROUTERS
# ============================================================

def architecture_router(state: ArchitectureSDLCState):
    return "quality" if state.get("change_required") else "review"


def review_router(state: ArchitectureSDLCState):
    if state["review"]["status"] == "PASS":
        return "release_planning"
    if (state.get("breaker") or {}).get("level") == breaker.STOP:
        return "finalize"
    if not _can_retry(state):
        return "finalize"
    return "architecture"


# ============================================================
# GRAPH
# ============================================================

def build_graph():
    graph = StateGraph(ArchitectureSDLCState)

    graph.add_node("requirement", requirement_node)
    graph.add_node("architecture", architecture_node)
    graph.add_node("quality", quality_node)
    graph.add_node("implementation_spec", implementation_spec_node)
    graph.add_node("review", review_node)
    graph.add_node("release_planning", release_planning_node)
    graph.add_node("release_apply", release_apply_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "requirement")
    graph.add_edge("requirement", "architecture")

    graph.add_conditional_edges(
        "architecture",
        architecture_router,
        {"quality": "quality", "review": "review"},
    )

    graph.add_edge("quality", "implementation_spec")
    graph.add_edge("implementation_spec", "review")

    graph.add_conditional_edges(
        "review",
        review_router,
        {
            "release_planning": "release_planning",
            "architecture": "architecture",
            "finalize": "finalize",
        },
    )

    graph.add_edge("release_planning", "release_apply")
    graph.add_edge("release_apply", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()
