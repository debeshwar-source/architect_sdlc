"""
SDLC planning pipeline, wired as a LangGraph state machine.

    requirement -> design -> [change required?]
                                |-> release_planning -> release_apply -> jira_publish -> kb_save -> finalize
                                '-> finalize (design concluded nothing needs to change)

The user may also choose WHICH agents run: start with the Requirement agent or
skip straight to the Design agent (their text is then used as the requirement,
as written), and stop after the Requirement agent, the Design agent, or carry on
through release. Between the two agents they can add binding architectural
direction for the Design agent. See `start_at`, `stop_after` and
`architecture_direction` in the state below.

release_apply writes the local files (mock Jira + knowledge-base updates).
jira_publish then creates the tickets in the REAL Jira, but only if Jira is
configured AND a human approves; otherwise it is skipped and the local files
remain the deliverable.
kb_save is the last step: it offers to save the knowledge-base documents the
Design agent proposed - as a NEW knowledge base, or into the one the user
selected - and writes nothing unless the user says yes.

Three agents do the work: Requirement, Design and Release. A run uses ONE
knowledge base chosen by the user (state `kb_name`) or none ("create
something new" = from scratch, existing knowledge bases withheld). The
Design agent can propose knowledge-base documents; kb_save offers to save
them once the release is done.

Deliverable is the plan + Jira epic/stories/tasks/subtasks -- this
orchestrator does not write code.

Plain words: this file is the "conductor". It decides the ORDER in which
the specialist agents run (requirement -> design -> release), passes each
stage's results to the next stage, and keeps the run's diary (events) and
shared whiteboard (blackboard) up to date.

Vocabulary used below:
  node   = one step of the pipeline (usually one agent)
  state  = the shared bundle of data passed from step to step
  router = a small decision function: "which step comes next?"
"""

import copy
import functools
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

import agents.design as design_agent
import agents.release as release_agent
import agents.requirement as requirement_agent
import config
import knowledge_base.loader as kb_loader
import tools.issue_tracker as issue_tracker
import tools.jira_client as jira_client
from hive import blackboard, events, memory
from knowledge_base.loader import ApplicationDoc


# The "state": a labelled bundle of everything known about the run so far.
# Each step reads what it needs from it and returns new/changed fields.
# (total=False simply means not every field exists from the very start.)
class SDLCState(TypedDict, total=False):
    run_id: str                          # unique ID of this run
    requirement_text: str                # the user's original request
    # The knowledge base documents. May be an empty list: the agents then
    # design from scratch.
    applications: List[ApplicationDoc]

    # Which agents run, chosen by the user up front:
    #   start_at    "requirement" (default) runs the Requirement agent first;
    #               "design" skips it and uses the user's text AS the requirement
    #   stop_after  "requirement" | "design" | "release" (default): the last stage
    #               to run. Stopping after "design" still offers to save the
    #               proposed knowledge-base documents, but creates no Jira backlog.
    #   requirement_skipped  set when the Requirement agent was skipped
    start_at: str
    stop_after: str
    requirement_skipped: bool

    # Architectural changes the user makes BETWEEN the Requirement and Design
    # agents (typed in the form, or at the pause after the Requirement agent):
    # binding direction about HOW to design the solution, e.g. "use the existing
    # event bus; no new databases". Handed to the Design agent on every pass.
    architecture_direction: str

    # FROM-SCRATCH builds: the existing knowledge base (and recalled past
    # runs) must play no part.
    #   force_from_scratch  the user's up-front choice (UI checkbox / CLI flag)
    #   from_scratch        the EFFECTIVE value every stage obeys: the user's
    #                       choice OR the Requirement agent's `build_mode`.
    #                       Recomputed whenever the requirement stage runs, so
    #                       a reviewer correcting a wrong guess takes effect.
    force_from_scratch: bool
    from_scratch: bool

    # Each stage's finished output:
    requirement: dict
    design: dict
    change_required: bool                # does the design need any change at all?
    release_plan: dict                   # epic, stories/tasks/subtasks, rollout
    release_result: dict                 # the Jira files written (mock Jira)
    kb_result: dict                      # what kb_save did (status, target, per-document results)

    # Which knowledge base this run uses:
    #   kb_name         the NAME of the existing knowledge base the user selected
    #                   (its applications were loaded into `applications`), or ""
    #                   when the user chose "create something new".
    #   kb_approved     True / False once the user has decided whether to save the
    #                   proposed knowledge-base documents; None = not asked yet
    #   new_kb_name     the name the user gave the NEW knowledge base (when saving one)
    #   kb_confirm      optional function(preview) -> name / True / falsy that
    #                   kb_save calls to ASK (the CLI supplies one; the web UI asks
    #                   on screen and sets kb_approved / new_kb_name itself)
    kb_name: str
    kb_approved: Any
    new_kb_name: str
    kb_confirm: Callable[[dict], Any]

    # Creating tickets in the real Jira is outward-facing, so it only happens
    # with explicit approval:
    #   jira_approved  True / False once a human has decided; None = not asked yet
    #   jira_selection which items the human ticked (keys like "epic", "S1",
    #                  "S1/T2"; see tools/jira_client.plan_keys); None = all of them
    #   jira_confirm   optional function(preview) the pipeline calls to ASK (the
    #                  CLI supplies one; the web UI instead asks on screen and
    #                  sets jira_approved / jira_selection itself). It returns
    #                  True (create everything), a collection of keys (create
    #                  just those), or a falsy value (create nothing).
    #   jira_result    what happened: status created / declined / skipped / failed
    jira_approved: Any
    jira_selection: Any
    jira_confirm: Callable[[dict], bool]
    jira_result: dict

    # Set by the Streamlit UI's human-in-the-loop mode when a reviewer
    # asks a stage to revise its own last output, e.g.
    # {"stage": "design", "note": "..."}. A node consumes this via
    # _revision_feedback_for only when it matches that node's own stage
    # name; the CLI (no HITL) never sets this.
    pending_human_note: dict

    iteration: int        # how many design passes have happened so far
    run_outcome: str      # how the run ended (e.g. APPROVED_RELEASED)
    history: List[dict]   # step-by-step record of every stage that ran

    # Per-agent Claude model override for this run, keyed by
    # config.AGENT_NAMES (e.g. {"design": "claude-opus-5-5"}) -- set once
    # at run start (Streamlit's form / cli.py's --agent-model) and read
    # uniformly via _model_for. An agent missing from this dict falls back
    # to config.MODEL_ID.
    agent_models: Dict[str, str]


def _is_scratch(state: SDLCState) -> bool:
    # Is this a from-scratch build? Yes if the user forced it or the
    # Requirement agent decided so.
    return bool(state.get("from_scratch") or state.get("force_from_scratch"))


def append_history(state: SDLCState, entry: dict) -> List[dict]:
    # Return a copy of the history with one more entry at the end (the
    # original is left unchanged).
    history = list(state.get("history", []))
    history.append(entry)
    return history


def _model_for(state: SDLCState, agent_key: str) -> str:
    # Which AI model should this agent use? The user's per-agent choice if
    # there is one, otherwise the default from config.py.
    return (state.get("agent_models") or {}).get(agent_key) or config.MODEL_ID


def new_run_id() -> str:
    # Make a unique ID like "20260101-120000-a1b2c3": current UTC date/time
    # plus 6 random characters (so two runs in the same second differ).
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:6]}"


# ============================================================
# HIVE INSTRUMENTATION
#
# Every node is wrapped by _hive(stage): it logs stage start/finish to
# the run's event log, mirrors the stage's output onto the blackboard,
# and turns a consumed human note into a standing human decision. This
# lives here (not in the CLI/Streamlit) so both entry points get it.
# ============================================================

# Most stages store their output under a key equal to the stage name; this
# lists the exceptions: the release_planning stage stores "release_plan".
_RESULT_KEY = {"release_planning": "release_plan"}


def _context_for(stage: str, state: SDLCState) -> str:
    """Hive background for a stage's prompt: standing human decisions
    for that stage, plus (for the two framing stages) recalled similar
    past runs. Empty string when there is nothing to add."""
    # Start with any standing instructions a human gave this stage earlier.
    notes = blackboard.format_for_prompt(state["run_id"], stage)
    # The first two stages also get "lessons" from similar past runs - but
    # not in a from-scratch build, where past projects must not leak in. (The
    # requirement stage only knows the user's own choice at that point.)
    scratch = (
        state.get("force_from_scratch")
        if stage == "requirement"
        else state.get("from_scratch") or state.get("force_from_scratch")
    )
    if stage in ("requirement", "design") and not scratch:
        notes += memory.format_for_prompt(state["requirement_text"], state["run_id"])
    # The user's own architectural direction goes to the Design agent only.
    direction = (state.get("architecture_direction") or "").strip()
    if stage == "design" and direction:
        notes += f"""
============================================================
ARCHITECTURAL DIRECTION FROM THE USER (binding)
============================================================

The user gave this direction about HOW the solution should be designed. Follow
it. If it conflicts with the requirement or with the knowledge base, still
follow the direction and record the conflict as a concrete entry in
`architecture_risks`. Do not ask about it again.

{direction}
"""
    return notes


def _summarize(stage: str, result: dict) -> str:
    # One-line, human-friendly description of what a stage produced, used
    # in the event diary (e.g. "3 stories", "change_required=True; affected: ...").
    out = result.get(_RESULT_KEY.get(stage, stage)) or {}
    if stage == "requirement":
        return out.get("title", "")
    if stage == "design":
        names = [a.get("name", "") for a in out.get("affected_applications", [])]
        new = [a.get("name", "") for a in out.get("new_applications_needed", [])]
        return (
            f"change_required={out.get('change_required')}; affected: {', '.join(names) or 'none'}; "
            f"new: {', '.join(new) or 'none'}; kb updates: {len(out.get('knowledge_base_updates', []))}"
        )
    if stage == "release_planning":
        stories = out.get("stories", [])
        tasks = sum(len(s.get("tasks", [])) for s in stories)
        return f"{len(stories)} story(ies), {tasks} task(s)"
    if stage == "release_apply":
        counts = (result.get("release_result") or {}).get("counts", {})
        return ", ".join(f"{n} {k}" for k, n in counts.items())
    if stage == "kb_save":
        kb = result.get("kb_result") or {}
        return f"{kb.get('status')}: {kb.get('kb_name') or kb.get('reason') or kb.get('error', '')}"
    if stage == "jira_publish":
        jira = result.get("jira_result") or {}
        counts = jira.get("counts", {})
        extra = ", ".join(f"{n} {k}" for k, n in counts.items()) or jira.get("reason") or jira.get("error", "")
        return f"{jira.get('status')}: {extra}"
    if stage == "finalize":
        return result.get("run_outcome", "")
    return ""


def _post_stage_output(run_id: str, stage: str, result: dict, iteration: int) -> None:
    # Copy the important parts of a stage's output onto the shared
    # whiteboard: its decisions, the risks it saw, and its open questions.
    out = result.get(_RESULT_KEY.get(stage, stage))
    if not isinstance(out, dict):
        return
    # This stage is giving a fresh answer, so mark its old notes outdated.
    blackboard.supersede(run_id, stage)

    if stage == "requirement":
        blackboard.post(run_id, "decisions", stage, out.get("scope_summary"), iteration)
    elif stage == "design":
        names = [a.get("name", "") for a in out.get("affected_applications", [])]
        new = [a.get("name", "") for a in out.get("new_applications_needed", [])]
        blackboard.post(
            run_id, "decisions", stage,
            f"change_required={out.get('change_required')}; affected: {', '.join(names) or 'none'}; "
            f"new: {', '.join(new) or 'none'}; "
            f"order: {' -> '.join(map(str, out.get('dependency_order', [])))}",
            iteration,
        )
        for risk in out.get("architecture_risks", []):
            blackboard.post(
                run_id, "risks", stage,
                risk if isinstance(risk, str) else json.dumps(risk, default=str), iteration,
            )

    # Any question the stage wants a human to answer goes on the board too.
    for q in out.get("clarifying_questions", []):
        blackboard.post(run_id, "open_questions", stage, q.get("question"), iteration)


def _hive(stage: str):
    # A "decorator": a wrapper placed around a step so that, without
    # changing the step itself, extra bookkeeping happens before and after
    # it (write to the diary, update the whiteboard, etc.).
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(state: SDLCState):
            run_id = state["run_id"]
            # Did a human leave a note aimed at THIS stage?
            pending = state.get("pending_human_note")
            human_note = pending["note"] if pending and pending.get("stage") == stage else None

            # Diary: the stage is starting.
            events.emit(run_id, "stage_started", stage, iteration=state.get("iteration", 0))
            try:
                # Run the real step.
                result = fn(state)
            except Exception as exc:
                # Diary: it failed. Then re-raise so the failure is not hidden.
                events.emit(run_id, "stage_failed", stage, error=str(exc))
                raise

            iteration = result.get("iteration", state.get("iteration", 0))
            # If a human note was used: mark the stage's questions answered,
            # and record the note as a lasting decision for later passes.
            if human_note:
                blackboard.resolve_questions(run_id, stage)
                blackboard.post(run_id, "human_decisions", stage, human_note, iteration)
                events.emit(run_id, "human_note", stage, note=human_note)

            # Update the whiteboard and write "stage done" to the diary.
            _post_stage_output(run_id, stage, result, iteration)
            events.emit(run_id, "stage_done", stage, iteration=iteration, summary=_summarize(stage, result))
            return result

        return wrapper

    return decorator


# ============================================================
# NODES
#
# Each "node" below is one pipeline step. They all follow the same
# pattern: get any revision feedback -> call the matching agent with the
# data it needs -> return the new result (plus a history entry).
# ============================================================

def direct_requirement(text: str, from_scratch: bool) -> dict:
    """The user's text turned into a requirement WITHOUT the Requirement agent
    (used when they start at the Design agent). Nothing is added or interpreted:
    the text is the scope, exactly as written."""
    text = text.strip()
    first_line = text.splitlines()[0] if text else ""
    return {
        "title": first_line[:80],
        "business_goal": "",
        "scope_summary": text,
        "acceptance_criteria": [],
        "out_of_scope": [],
        "build_mode": "from_scratch" if from_scratch else "extend_existing",
        "clarifying_questions": [],
    }


@_hive("requirement")
def requirement_node(state: SDLCState):
    # STEP 1: turn the raw request into a structured requirement - unless the
    # user chose to start at the Design agent, in which case their text is used
    # as the requirement exactly as written and no AI call is made.
    if state.get("start_at") == "design":
        forced = bool(state.get("force_from_scratch"))
        requirement = direct_requirement(state["requirement_text"], forced)
        return {
            "requirement": requirement,
            "requirement_skipped": True,
            "from_scratch": forced,
            "history": append_history(
                state, {"stage": "requirement", "status": "SKIPPED", "result": requirement}
            ),
        }
    revision_feedback = _revision_feedback_for("requirement", state)
    requirement = requirement_agent.run(
        state["requirement_text"], state["applications"], revision_feedback,
        model=_model_for(state, "requirement"),
        context_notes=_context_for("requirement", state),
        from_scratch=bool(state.get("force_from_scratch")),
    )
    return {
        "requirement": requirement,
        # From-scratch if the user said so up front, or the Requirement agent
        # judged the request to be a from-scratch build.
        "from_scratch": bool(state.get("force_from_scratch")) or requirement.get("build_mode") == "from_scratch",
        "history": append_history(
            state, {"stage": "requirement", "status": "DONE", "result": requirement}
        ),
    }


@_hive("design")
def design_node(state: SDLCState):
    # STEP 2: decide what to build - which applications change or are
    # created, in what order, how it is tested, and how the knowledge base
    # should be created/augmented.
    #
    # A human revising the design in place (via the HITL "answer" gate) is
    # not a new pass, so it does not advance the iteration counter.
    pending = state.get("pending_human_note")
    is_human_revision = bool(pending and pending.get("stage") == "design")
    iteration = state.get("iteration", 0) if is_human_revision else state.get("iteration", 0) + 1

    revision_feedback = _revision_feedback_for("design", state)

    # Show the user's architectural direction on the shared whiteboard (replacing
    # an earlier version of it, if they changed it), and note it in the diary.
    direction = (state.get("architecture_direction") or "").strip()
    if direction:
        blackboard.supersede(state["run_id"], "direction", sections=("human_decisions",))
        blackboard.post(state["run_id"], "human_decisions", "direction", direction, iteration)
        events.emit(state["run_id"], "direction", "design", direction=direction)

    design = design_agent.run(
        state["requirement"], state["applications"], revision_feedback,
        model=_model_for(state, "design"),
        context_notes=_context_for("design", state),
        from_scratch=_is_scratch(state),
    )

    return {
        "design": design,
        "change_required": design["change_required"],
        "iteration": iteration,
        # A fresh design supersedes every downstream artifact - without
        # clearing these, a revised design (e.g. one that now decides no
        # change is needed) would leave a stale release plan in state.
        "release_plan": {},
        "release_result": {},
        "kb_result": {},
        "kb_approved": None,
        # Any earlier Jira approval and ticket choice were for the earlier plan, not this one.
        "jira_approved": None,
        "jira_selection": None,
        "jira_result": {},
        "history": append_history(
            state,
            {
                "stage": "design",
                "status": "DONE",
                "iteration": iteration,
                "result": design,
            },
        ),
    }


@_hive("release_planning")
def release_planning_node(state: SDLCState):
    # STEP 3: plan the rollout and draft the Jira epic, stories, tasks
    # and subtasks.
    revision_feedback = _revision_feedback_for("release_planning", state)
    release_plan = release_agent.run(
        state["requirement"], state["design"], revision_feedback,
        model=_model_for(state, "release"),
        context_notes=_context_for("release_planning", state),
    )
    return {
        "release_plan": release_plan,
        # A new or revised plan needs a fresh approval (and a fresh choice of
        # tickets) before it goes to Jira.
        "jira_approved": None,
        "jira_selection": None,
        "jira_result": {},
        "history": append_history(
            state, {"stage": "release_planning", "status": "DONE", "result": release_plan}
        ),
    }


@_hive("release_apply")
def release_apply_node(state: SDLCState):
    # STEP 4: write the Jira epic with all its stories, tasks and subtasks
    # as local files (the mock Jira). Nothing outside this run's own folder
    # is touched here; the real Jira and the knowledge base come later, each
    # only after the user agrees.
    run_dir = config.RUNS_ROOT / state["run_id"]
    release_result = issue_tracker.write_release(
        run_dir / "jira_mock",
        state["release_plan"]["epic"],
        state["release_plan"]["stories"],
        state["run_id"],
    )
    return {
        "release_result": release_result,
        "history": append_history(
            state, {"stage": "release_apply", "status": "PASS", "result": release_result}
        ),
    }


# ============================================================
# EDITING THE WORDING OF THE TICKETS
#
# Before anything goes to Jira the user may reword the tickets (the web UI's
# Jira step offers this). Only WORDS can change - titles, descriptions,
# acceptance criteria and sub-task steps. Priority, points, labels, links,
# which stories exist and how many tasks they have are all fixed, so an edit
# can never alter the shape of the plan.
# ============================================================

_EPIC_TEXT = ("title", "description")
_STORY_TEXT = ("title", "description", "acceptance_criteria")
_TASK_TEXT = ("title", "description")


def _everything_but_wording(plan: dict) -> dict:
    # A copy of the plan with the editable wording removed - what is left must
    # be identical before and after an edit.
    skeleton = copy.deepcopy(plan)
    for key in _EPIC_TEXT:
        (skeleton.get("epic") or {}).pop(key, None)
    for story in skeleton.get("stories", []):
        for key in _STORY_TEXT:
            story.pop(key, None)
        for task in story.get("tasks", []):
            for key in _TASK_TEXT:
                task.pop(key, None)
            task.pop("subtasks", None)        # their wording (and count) is editable
    return skeleton


def plan_text_changes(old: dict, new: dict) -> List[str]:
    """Which pieces of wording differ, as short names: "epic.title",
    "S1.description", "S1.acceptance_criteria", "S1/T2.title", "S1/T2.steps"."""
    changes = [f"epic.{k}" for k in _EPIC_TEXT if (old.get("epic") or {}).get(k) != (new.get("epic") or {}).get(k)]
    for o, n in zip(old.get("stories", []), new.get("stories", [])):
        changes += [f"{o['id']}.{k}" for k in _STORY_TEXT if o.get(k) != n.get(k)]
        for i, (ot, nt) in enumerate(zip(o.get("tasks", []), n.get("tasks", [])), start=1):
            changes += [f"{o['id']}/T{i}.{k}" for k in _TASK_TEXT if ot.get(k) != nt.get(k)]
            if ot.get("subtasks") != nt.get("subtasks"):
                changes.append(f"{o['id']}/T{i}.steps")
    return changes


def apply_ticket_edits(state: SDLCState, edited_plan: dict) -> dict:
    """Accept the user's reworded release plan. Returns the state update (empty
    if nothing changed). Raises ValueError, changing nothing, if the edit would
    alter anything but wording or leave a ticket without a title.

    The local Jira files are rewritten too, so they always say the same thing
    as what is sent to Jira."""
    old = state["release_plan"]
    if _everything_but_wording(old) != _everything_but_wording(edited_plan):
        raise ValueError("Only the wording of tickets can be edited, nothing else about the plan.")
    blank = []
    if not str((edited_plan.get("epic") or {}).get("title", "")).strip():
        blank.append("the epic")
    for story in edited_plan.get("stories", []):
        if not str(story.get("title", "")).strip():
            blank.append(f"story {story['id']}")
        blank += [f"a task of {story['id']}" for t in story.get("tasks", []) if not str(t.get("title", "")).strip()]
        for task in story.get("tasks", []):
            blank += [f"a step of {story['id']}" for s in task.get("subtasks", []) if not str(s.get("title", "")).strip()]
    if blank:
        raise ValueError("Every ticket needs a title; missing for: " + ", ".join(blank))

    changes = plan_text_changes(old, edited_plan)
    if not changes:
        return {}

    update: Dict[str, Any] = {"release_plan": edited_plan}
    if state.get("release_result"):
        update["release_result"] = issue_tracker.write_release(
            config.RUNS_ROOT / state["run_id"] / "jira_mock",
            edited_plan["epic"], edited_plan["stories"], state["run_id"],
        )
    events.emit(state["run_id"], "plan_edited", "release_planning", changes=len(changes), fields=changes[:40])
    update["history"] = append_history(
        state, {"stage": "ticket_edit", "status": "EDITED", "result": {"changes": changes}}
    )
    return update


@_hive("jira_publish")
def jira_publish_node(state: SDLCState):
    # STEP 5: create the tickets in the REAL Jira - only with approval.
    #   * Jira not configured          -> skipped (the local files remain)
    #   * no approval / human said no  -> declined (nothing is sent to Jira)
    #   * approved                     -> create the tickets the human CHOSE
    #                                     (all of them unless they picked some);
    #                                     on a Jira error, record the failure (and
    #                                     what was already created) instead of
    #                                     crashing the run
    plan = state["release_plan"]
    preview = jira_client.preview(plan)

    approved = state.get("jira_approved")
    selection = state.get("jira_selection")          # None = everything
    if preview["configured"] and approved is None and callable(state.get("jira_confirm")):
        # Ask the human now (the CLI path). Their answer is recorded in state.
        answer = state["jira_confirm"](preview)
        if isinstance(answer, (list, tuple, set, frozenset)):
            approved, selection = True, set(answer)             # they picked tickets (maybe none)
        else:
            approved, selection = bool(answer), None            # yes = everything, no = nothing

    # Only keys that exist in this plan count; nothing left means nothing chosen.
    chosen = None if selection is None else set(selection) & set(jira_client.plan_keys(plan))

    if not preview["configured"]:
        result = {"status": "skipped",
                  "reason": "Jira is not configured (missing: " + ", ".join(preview["missing"]) + ")."}
    elif not approved:
        result = {"status": "declined",
                  "reason": "Not approved, so nothing was sent to Jira. The backlog is saved locally."}
    elif chosen is not None and not chosen:
        result = {"status": "declined",
                  "reason": "No tickets were selected, so nothing was sent to Jira. The backlog is saved locally."}
    else:
        try:
            result = jira_client.from_env().publish(
                plan["epic"], plan["stories"], state["run_id"],
                state_path=config.RUNS_ROOT / state["run_id"] / "jira_created.json",
                selection=chosen,
            )
        except jira_client.JiraError as exc:
            result = {"status": "failed", "error": str(exc), "created_count": exc.created_count}

    return {
        "jira_approved": approved,
        "jira_selection": selection,
        "jira_result": result,
        "history": append_history(
            state, {"stage": "jira_publish", "status": result["status"].upper(), "result": result}
        ),
    }


def kb_save_preview(state: SDLCState) -> dict:
    """What saving the knowledge base WOULD do - shown when asking the user.
    No files are touched.

    mode "new"       the run was from scratch: the proposed documents would form
                     a NEW knowledge base (the user picks its name)
    mode "existing"  the user selected a knowledge base: the documents would be
                     added to it (new ones created, existing ones appended to)
    mode "none"      the design proposed nothing to save"""
    updates = (state.get("design") or {}).get("knowledge_base_updates", [])
    if not updates:
        return {"mode": "none", "updates": []}

    existing = kb_loader.list_knowledge_bases()
    if _is_scratch(state) or not state.get("kb_name"):
        return {
            "mode": "new",
            "kb_name": "",
            "suggested_name": kb_loader.suggest_kb_name((state.get("requirement") or {}).get("title", "")),
            "existing_names": [kb.name for kb in existing],
            "updates": [{"application": u["application"], "action": "create", "reason": u.get("reason", ""),
                         "content": u["content"]} for u in updates],
        }
    return {
        "mode": "existing",
        "kb_name": state["kb_name"],
        "updates": [{"application": u["application"], "action": u["action"], "reason": u.get("reason", ""),
                     "content": u["content"]} for u in updates],
    }


@_hive("kb_save")
def kb_save_node(state: SDLCState):
    # STEP 6: offer to save the knowledge-base documents the Design agent
    # proposed. NOTHING is written unless the user agrees:
    #   * no proposals                  -> nothing to ask
    #   * no approval / user said no    -> declined (nothing written)
    #   * approved, from-scratch run    -> create a NEW knowledge base folder
    #                                      (refusing a name that is taken) and
    #                                      write the documents into it
    #   * approved, existing selected   -> add to that knowledge base
    # A problem is recorded in the result instead of crashing the run.
    preview = kb_save_preview(state)

    if preview["mode"] == "none":
        result = {"status": "none", "reason": "The design proposed no knowledge-base changes."}
        return _kb_return(state, result, None)

    approved = state.get("kb_approved")
    chosen_name = state.get("new_kb_name") or ""
    if approved is None and callable(state.get("kb_confirm")):
        # Ask the user now (the CLI path). The answer may be the new name.
        answer = state["kb_confirm"](preview)
        approved = bool(answer)
        if isinstance(answer, str) and answer.strip():
            chosen_name = answer.strip()

    if not approved:
        result = {"status": "declined", "mode": preview["mode"],
                  "reason": "Not saved. The proposed documents remain in this run's history.json."}
        return _kb_return(state, result, approved)

    try:
        if preview["mode"] == "new":
            name = chosen_name or preview["suggested_name"]
            kb_dir = kb_loader.create_knowledge_base(name)          # refuses an existing name
            results = kb_loader.apply_updates(
                (state.get("design") or {}).get("knowledge_base_updates", []),
                run_id=state["run_id"], kb_dir=kb_dir, create_only=True,
            )
            created_new = True
        else:
            kb_dir = kb_loader.kb_path(preview["kb_name"])
            results = kb_loader.apply_updates(
                (state.get("design") or {}).get("knowledge_base_updates", []),
                run_id=state["run_id"], kb_dir=kb_dir, create_only=False,
            )
            created_new = False
        result = {"status": "saved", "mode": preview["mode"], "kb_name": kb_dir.name,
                  "path": str(kb_dir), "created_new_kb": created_new, "results": results}
    except (OSError, ValueError) as exc:
        result = {"status": "failed", "mode": preview["mode"], "error": str(exc)}
    return _kb_return(state, result, approved)


def _kb_return(state: SDLCState, result: dict, approved) -> dict:
    return {
        "kb_result": result,
        "kb_approved": approved,
        "history": append_history(
            state, {"stage": "kb_save", "status": result["status"].upper(), "result": result}
        ),
    }


@_hive("finalize")
def finalize_node(state: SDLCState):
    # STEP 5 (always last): work out how the run ended and save a summary
    # of it to long-term memory for future runs.
    outcome = _derive_run_outcome(state)
    history = append_history(
        state, {"stage": "finalize", "status": outcome, "result": {"run_outcome": outcome}}
    )
    memory.remember({**state, "run_outcome": outcome, "history": history})
    return {"run_outcome": outcome, "history": history}


def _revision_feedback_for(stage: str, state: SDLCState) -> dict:
    # Work out whether this stage should be told "redo your work, here is
    # what a human wants changed". Returns None on a normal first attempt.
    # The only source of feedback is a note a human left for this stage.
    pending = state.get("pending_human_note")
    if pending and pending.get("stage") == stage:
        return {
            "stage_being_revised": stage,
            "human_feedback": [
                {
                    "id": "human-note",
                    "severity": "BLOCKING",
                    "finding": pending["note"],
                    "evidence": "Note from a human reviewer",
                    "suggested_fix": "",
                }
            ],
        }
    return None


def _derive_run_outcome(state: SDLCState) -> str:
    # Give the run a final label, checked in this order:
    #  RELEASED_TO_JIRA    - the tickets were created in the real Jira
    #  JIRA_FAILED         - approved, but creating them in Jira failed (the
    #                        backlog is still saved locally; publishing can be retried)
    #  APPROVED_RELEASED   - the backlog was saved locally (Jira not configured, or
    #                        not approved)
    #  NO_CHANGE_REQUIRED  - the design concluded nothing needs to change
    #  DESIGN_ONLY         - the user chose to stop after the Design agent
    #  REQUIREMENT_ONLY    - the user chose to stop after the Requirement agent
    #  INCOMPLETE          - anything else
    jira_status = (state.get("jira_result") or {}).get("status")
    if state.get("release_result") and jira_status == "created":
        return "RELEASED_TO_JIRA"
    if state.get("release_result") and jira_status == "failed":
        return "JIRA_FAILED"
    if state.get("release_result"):
        return "APPROVED_RELEASED"

    if state.get("design") and not state.get("change_required"):
        return "NO_CHANGE_REQUIRED"

    if state.get("design") and state.get("stop_after") == "design":
        return "DESIGN_ONLY"
    if state.get("requirement") and not state.get("design") and state.get("stop_after") == "requirement":
        return "REQUIREMENT_ONLY"

    return "INCOMPLETE"


# ============================================================
# ROUTERS
#
# A router answers "what is the next step?" after a node finishes.
# ============================================================

def requirement_router(state: SDLCState):
    # Normally the Design agent is next. If the user chose to stop after the
    # Requirement agent, the run ends here.
    return "finalize" if state.get("stop_after") == "requirement" else "design"


def design_router(state: SDLCState):
    # No change needed -> nothing to release, so go straight to the end.
    if not state.get("change_required"):
        return "finalize"
    # The user chose to stop after the Design agent: no release plan or Jira
    # backlog, but the last step still offers to save the knowledge-base
    # documents the design proposed.
    if state.get("stop_after") == "design":
        return "kb_save"
    return "release_planning"


# ============================================================
# GRAPH
# ============================================================

def build_graph():
    """Assemble the pipeline: register the steps, then draw the arrows
    between them, and return a ready-to-run pipeline."""
    graph = StateGraph(SDLCState)

    # Register every step by name.
    graph.add_node("requirement", requirement_node)
    graph.add_node("design", design_node)
    graph.add_node("release_planning", release_planning_node)
    graph.add_node("release_apply", release_apply_node)
    graph.add_node("jira_publish", jira_publish_node)
    graph.add_node("kb_save", kb_save_node)
    graph.add_node("finalize", finalize_node)

    # Fixed arrows ("edge" = always go from A to B):
    graph.add_edge(START, "requirement")

    # A conditional arrow uses a router function to choose the next step.
    # After requirement: the design, or the end if the user stops there.
    graph.add_conditional_edges(
        "requirement",
        requirement_router,
        {"design": "design", "finalize": "finalize"},
    )
    # After design: release planning, the knowledge-base step (if the user
    # stops after design), or straight to the end (nothing to change).
    graph.add_conditional_edges(
        "design",
        design_router,
        {"release_planning": "release_planning", "kb_save": "kb_save", "finalize": "finalize"},
    )

    # The final stretch is always in this fixed order.
    graph.add_edge("release_planning", "release_apply")
    graph.add_edge("release_apply", "jira_publish")
    graph.add_edge("jira_publish", "kb_save")
    graph.add_edge("kb_save", "finalize")
    graph.add_edge("finalize", END)

    # "Compile" turns the drawing into something that can be executed.
    return graph.compile()
