"""
CLI entrypoint for the SDLC planning orchestrator.

Takes a requirement, reasons over the knowledge base of existing
applications (optional -- with none it designs from scratch), and
produces a design plus a Jira epic with stories, tasks and subtasks. The
Design agent may also create new knowledge-base documents or append to
existing ones.

Usage:
    python3 cli.py --requirement "Notify users by email/push when a \\
        new episode of a show they're watching becomes available,
        respecting their existing notification preferences."

    python3 cli.py --requirement @requirement.txt

Which agents run: by default Requirement -> Design -> Release. --start-at design
skips the Requirement agent (your text is used as the requirement, as written);
--stop-after requirement|design ends the run early. --direction adds your own
architectural direction for the Design agent.

Knowledge bases: each .md file under knowledge_base/ is one (the demo one,
knowledge_base/streamly.md, describes a fictional OTT streaming platform). Pick
one with --kb NAME, or use --new-kb to start something new; the new knowledge
base is only saved if you say yes at the end. See --list-kbs.

Plain words: this is the command-line way to run the whole pipeline. You
type your request in a terminal; it runs every stage in order, prints a
short progress report as each finishes, and saves the results to files.
"""

import argparse
import sys
from pathlib import Path

import config
import knowledge_base.loader as kb_loader
import orchestrator.graph as graph
import run_log


def _load_requirement(arg: str) -> str:
    # If the argument starts with "@", treat the rest as a file name and
    # read the requirement from that file; otherwise it is the text itself.
    if arg.startswith("@"):
        return Path(arg[1:]).read_text(encoding="utf-8")
    return arg


def _parse_agent_model_overrides(pairs: list) -> dict:
    # Convert entries like "architecture=claude-opus-5-5" into a lookup
    # {"architecture": "claude-opus-5-5"}, rejecting malformed ones.
    overrides = {}
    for pair in pairs:
        # Must contain "=".
        if "=" not in pair:
            raise SystemExit(f"--agent-model expects AGENT=MODEL, got: {pair!r}")
        agent, model = (part.strip() for part in pair.split("=", 1))
        # The agent name must be one of the six known agents.
        if agent not in config.AGENT_NAMES:
            raise SystemExit(
                f"--agent-model: unknown agent {agent!r} -- choices: "
                f"{', '.join(config.AGENT_NAMES)}"
            )
        # The model name must not be blank.
        if not model:
            raise SystemExit(f"--agent-model {agent}=... needs a model id.")
        overrides[agent] = model
    return overrides


def _save_requirement_docs(run_id: str, requirement: dict, choice: str, kb_name: str) -> None:
    """Write the requirement as a Word document and/or PDF into the run's folder."""
    from tools import requirement_doc
    run_dir = config.RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "knowledge_base": kb_name}
    for fmt in (["docx", "pdf"] if choice == "both" else [choice]):
        try:
            data, file_name, _ = requirement_doc.build(requirement, meta, fmt)
        except requirement_doc.MissingLibrary as exc:
            print(f"    could not make the {fmt}: {exc}")
            continue
        (run_dir / file_name).write_bytes(data)
        print(f"    requirement document: {run_dir / file_name}")


def _save_design_docs(run_id: str, requirement: dict, design: dict, choice: str, kb_name: str) -> None:
    """Write the design (components, connections, diagrams) as a Word document and/or PDF into the run's folder."""
    from tools import design_doc
    run_dir = config.RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "knowledge_base": kb_name}
    for fmt in (["docx", "pdf"] if choice == "both" else [choice]):
        try:
            data, file_name, _ = design_doc.build(requirement, design, meta, None, fmt)
        except design_doc.MissingLibrary as exc:
            print(f"    could not make the {fmt}: {exc}")
            continue
        (run_dir / file_name).write_bytes(data)
        print(f"    design document: {run_dir / file_name}")


def _save_test_plan(run_id: str, requirement: dict, plan: dict) -> None:
    """Write the test plan as an Excel workbook into the run's folder."""
    from tools import test_plan_doc
    run_dir = config.RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        data, file_name, _ = test_plan_doc.build(requirement, plan, {"run_id": run_id})
    except test_plan_doc.MissingLibrary as exc:
        print(f"    could not make the test plan: {exc}")
        return
    (run_dir / file_name).write_bytes(data)
    print(f"    test plan: {run_dir / file_name}")


def _ask_to_publish_to_confluence(preview: dict) -> bool:
    """Called by the pipeline near the end when Confluence is set up. Shows where the design page would go
    and asks. A non-interactive run counts as no, so nothing is ever published unasked."""
    print("\n" + "-" * 70)
    print("PUBLISH THE DESIGN TO CONFLUENCE?")
    print("-" * 70)
    print(f"  {preview['base_url']}  space {preview['space_key']}"
          + (f"  under page {preview['parent_page_id']}" if preview["parent_page_id"] else ""))
    print(f"  page: {preview['title']}  ({preview['attachments']} diagram(s) attached)")
    if not sys.stdin.isatty():
        print("  Not an interactive terminal, so nothing will be published.")
        return False
    return input("  Publish? [y/N] ").strip().lower() in ("y", "yes")


def _ask_to_save_knowledge_base(preview: dict):
    """Called by the pipeline at the very end, when the Design agent proposed
    knowledge-base documents. Shows them and asks whether to save. Returns the
    new knowledge base's name (or True for an existing one) to save, or a falsy
    value for no. Nothing is written without an answer; a non-interactive run
    (no terminal to ask on) counts as no."""
    print("\n" + "-" * 70)
    if preview["mode"] == "new":
        print("SAVE A NEW KNOWLEDGE BASE?")
    else:
        print(f"ADD TO THE KNOWLEDGE BASE '{preview['kb_name']}'?")
    print("-" * 70)
    for u in preview["updates"]:
        verb = "create " if u["action"] == "create" else "append to"
        print(f"  {verb} {u['application']}" + (f"  - {u['reason']}" if u["reason"] else ""))
    if not sys.stdin.isatty():
        print("  Not an interactive terminal, so nothing will be saved.")
        return None
    if preview["mode"] == "new":
        taken = set(preview["existing_names"])
        while True:
            answer = input(f"  Name for the new knowledge base (Enter = '{preview['suggested_name']}', n = don't save): ").strip()
            if answer.lower() in ("n", "no"):
                return None
            name = answer or preview["suggested_name"]
            if name in taken:
                print(f"  '{name}' already exists - choose a different name.")
                continue
            return name
    return input("  Add them? [y/N] ").strip().lower() in ("y", "yes")


STAGE_ORDER = ["requirement", "design", "release"]


def check_scope(start_at: str, stop_after: str) -> None:
    """The run must stop at or after the stage it starts at."""
    if STAGE_ORDER.index(stop_after) < STAGE_ORDER.index(start_at):
        raise SystemExit(
            f"--stop-after {stop_after} comes before --start-at {start_at}: "
            f"there would be nothing to run."
        )


def parse_jira_choice(text: str, preview: dict):
    """Turn what the user typed at the Jira prompt into a decision.

        n / Enter / anything unrecognised   ->  None  (create nothing)
        a, all, y, yes                      ->  True  (create everything)
        1,3                                 ->  a set of keys: stories 1 and 3 with ALL
                                                their sub-tasks, plus the epic
        1,3,x                               ->  the same, but leave out the epic

    Story numbers are the ones printed by the prompt (counting from 1). An
    unknown number makes the whole answer invalid (None), so a typo can never
    create something you did not mean to."""
    text = text.strip().lower()
    if text in ("a", "all", "y", "yes"):
        return True
    if not text or text in ("n", "no"):
        return None
    items = preview["items"]
    keys, with_epic = set(), True
    for token in (t.strip() for t in text.split(",")):
        if token == "x":
            with_epic = False
        elif token.isdigit() and 1 <= int(token) <= len(items):
            story = items[int(token) - 1]
            keys.add(story["key"])
            keys.update(t["key"] for t in story["tasks"])
        else:
            return None
    if not keys:
        return None
    if with_epic:
        keys.add("epic")
    return keys


def _ask_to_create_in_jira(preview: dict):
    """Called by the pipeline just before it would create tickets in the real
    Jira. Shows what could be created, and asks which to create. Returns True
    (everything), a set of keys (just those), or None (nothing). The answer
    defaults to NOTHING: Enter, anything unrecognised, or a non-interactive run
    (no terminal to ask on, e.g. a script or CI) means nothing is sent."""
    print("\n" + "-" * 70)
    print("PERMISSION NEEDED: create tickets in Jira?")
    print("-" * 70)
    print(f"  Jira:     {preview['base_url']}  (project {preview['project_key']})")
    print(f"  Account:  {preview['account']}")
    print(f"  Epic:     {preview['epic_title']}")
    for number, story in enumerate(preview["items"], start=1):
        print(f"  {number:>3}. {story['title']}  ({story['component']}, {len(story['tasks'])} sub-task(s))")
    print("  These are real tickets; undoing this means deleting them in Jira.")
    if not sys.stdin.isatty():
        print("  Not an interactive terminal, so nothing will be sent to Jira.")
        return None
    answer = input(
        "  Create which? a = all, numbers like 1,3 (with their sub-tasks), add x to leave out the epic, "
        "Enter = none: "
    )
    choice = parse_jira_choice(answer, preview)
    if choice is None and answer.strip().lower() not in ("", "n", "no"):
        print("  Not understood - nothing will be sent to Jira.")
    return choice


def _print_clarifying_questions(stage_result: dict) -> None:
    """The CLI has no human in the loop, so a clarifying question here
    is never answered -- just surfaced so it isn't silently lost. The
    agent has already proceeded on its own best judgment regardless
    (see skills/_common.py's CLARIFYING_QUESTIONS_RULE)."""
    for q in stage_result.get("clarifying_questions", []):
        print(f"    clarifying question: {q.get('question', '')}")
        options = q.get("options", [])
        if options:
            print(f"      options: {options} (recommended: {q.get('recommended_option', '')})")


def _print_node_update(node_name: str, node_result: dict) -> None:
    # Print a short progress summary for the stage that just finished.
    # Each branch below handles one stage and shows what matters for it.
    print(f"\n>>> {node_name}")

    if node_name == "requirement":
        req = node_result.get("requirement", {})
        if node_result.get("requirement_skipped"):
            print("    (Requirement agent skipped - your text is used as the requirement)")
        print(f"    title: {req.get('title', '')}")
        print(f"    build mode: {req.get('build_mode')}"
              + (" (existing knowledge base withheld)" if node_result.get("from_scratch") else ""))
        _print_clarifying_questions(req)

    elif node_name == "design":
        design = node_result.get("design", {})
        print(f"    change_required: {design.get('change_required')}")
        affected = design.get("affected_applications", [])
        if affected:
            print(f"    affected: {[a['name'] for a in affected]}")
        new_apps = design.get("new_applications_needed", [])
        if new_apps:
            print(f"    new applications proposed: {[a['name'] for a in new_apps]}")
        comps = design.get("components", [])
        if comps:
            print(f"    components ({len(comps)}): " + ", ".join(f"{c['name']} [{c['status']}]" for c in comps))
        for c in design.get("connections", []):
            print(f"      {c['from']} -> {c['to']}: {c['label']}")
        if design.get("dependency_order"):
            print(f"    dependency order: {design['dependency_order']}")
        for u in design.get("knowledge_base_updates", []):
            print(f"    knowledge base ({u['action']}): {u['application']}")
        _print_clarifying_questions(design)

    elif node_name == "quality_planning":
        plan = node_result.get("quality_plan", {})
        cases = plan.get("test_cases", [])
        print(f"    test cases: {len(cases)}")
        for c in cases:
            print(f"      {c.get('id')} [{c.get('priority')}] {c.get('type')}: {c.get('title')}")
        _print_clarifying_questions(plan)

    elif node_name == "release_planning":
        plan = node_result.get("release_plan", {})
        stories = plan.get("stories", [])
        tasks = sum(len(s.get("tasks", [])) for s in stories)
        subtasks = sum(len(t.get("subtasks", [])) for s in stories for t in s.get("tasks", []))
        print(f"    epic: {plan.get('epic', {}).get('title', '')}")
        print(f"    stories: {len(stories)}, tasks: {tasks}, subtasks: {subtasks}")
        print(f"    rollout stages: {len(plan.get('rollout_stages', []))}")
        _print_clarifying_questions(plan)

    elif node_name == "release_apply":
        result = node_result.get("release_result", {})
        print(f"    epic: {result.get('epic_id')} -> {result.get('epic_path')}")
        for story in result.get("stories", []):
            print(f"    story: {story['ticket_id']} ({story['component']}, {len(story['tasks'])} task(s))")

    elif node_name == "kb_save":
        kb = node_result.get("kb_result", {})
        status = kb.get("status")
        if status == "saved":
            what = "new knowledge base" if kb.get("created_new_kb") else "knowledge base"
            print(f"    saved to {what} '{kb['kb_name']}' ({kb['path']})")
            for r in kb["results"]:
                print(f"      {r['status']}: {r['action']} {r['application']}" + (f" - {r['detail']}" if r.get("detail") else ""))
        elif status == "failed":
            print(f"    FAILED: {kb.get('error')}")
        else:
            print(f"    {status}: {kb.get('reason', '')}")

    elif node_name == "jira_publish":
        jira = node_result.get("jira_result", {})
        status = jira.get("status")
        if status == "created":
            c = jira["counts"]
            print(f"    created in Jira: {c['epics']} epic, {c['stories']} stories, {c['sub_tasks']} sub-tasks")
            if jira.get("epic"):
                print(f"    epic: {jira['epic']['key']} -> {jira['epic']['url']}")
            for story in jira["stories"]:
                print(f"    story: {story['key']} {story['url']}")
            ns = jira.get("not_sent", {})
            if ns.get("stories") or ns.get("sub_tasks") or ns.get("epic"):
                print(f"    not sent (not selected): {len(ns.get('stories', []))} stories, "
                      f"{len(ns.get('sub_tasks', []))} sub-tasks" + (", the epic" if ns.get("epic") else ""))
            for d in jira.get("dropped_fields", []):
                print(f"    note: field {d['field']} not accepted by this project, left out")
            for w in jira.get("warnings", []):
                print(f"    warning: {w}")
        elif status == "failed":
            print(f"    FAILED: {jira.get('error')}")
            print(f"    ({jira.get('created_count', 0)} issue(s) were created before the failure; "
                  f"re-running the same plan resumes without duplicates)")
        else:
            print(f"    {status}: {jira.get('reason', '')}")

    elif node_name == "finalize":
        print(f"    outcome: {node_result.get('run_outcome')}")


def main() -> int:
    """Run the program. Returns 0 on success, 1 on failure (the standard
    way command-line programs report success/failure)."""
    # ---- 1. Define the command-line options the user may type ----
    parser = argparse.ArgumentParser(description="SDLC planning orchestrator: Requirement -> Design -> Quality -> Release")
    parser.add_argument(
        "--requirement", help="Requirement text, or @path/to/file"
    )
    parser.add_argument(
        "--requirement-doc",
        choices=["docx", "pdf", "both"],
        help="Also save the requirement as a Word document, a PDF, or both, in the run's folder.",
    )
    parser.add_argument(
        "--design-doc",
        choices=["docx", "pdf", "both"],
        help="Also save the design (components, connections, diagrams) as a Word document, a PDF, or both, in the run's folder.",
    )
    parser.add_argument(
        "--test-plan",
        action="store_true",
        help="Also save the Quality Planner's test cases as an Excel workbook in the run's folder.",
    )
    parser.add_argument(
        "--start-at",
        choices=["requirement", "design"],
        default="requirement",
        help="Start with the Requirement agent (default), or with the Design agent, "
             "using your text as the requirement exactly as written.",
    )
    parser.add_argument(
        "--stop-after",
        choices=["requirement", "design", "release"],
        default="release",
        help="Last stage to run (default: release). 'design' skips the Jira backlog but still "
             "offers to save the knowledge-base documents.",
    )
    parser.add_argument(
        "--direction",
        metavar="TEXT",
        help="Your architectural direction for the Design agent (binding), or @path/to/file.",
    )
    parser.add_argument(
        "--kb",
        metavar="NAME",
        help="Use this existing knowledge base (see --list-kbs).",
    )
    parser.add_argument(
        "--new-kb",
        "--from-scratch",
        dest="new_kb",
        action="store_true",
        help=(
            "Create something new: no existing knowledge base (and no past-run "
            "memory) is shown to the agents. A new knowledge base is offered at "
            "the end and saved only if you agree. (--from-scratch is the old "
            "name for this.) Without --kb or --new-kb, the only existing "
            "knowledge base is used; with several you must choose."
        ),
    )
    parser.add_argument(
        "--list-kbs", action="store_true", help="List the existing knowledge bases and exit."
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL_ID",
        help=(
            f"Default Claude model for every agent (default: "
            f"{config.MODEL_ID}, or $CLAUDE_SDLC_ARCHITECT_MODEL_ID). "
            f"Available: {', '.join(config.AVAILABLE_MODELS)}."
        ),
    )
    parser.add_argument(
        "--agent-model",
        action="append",
        default=[],
        metavar="AGENT=MODEL",
        help=(
            "Override the model for one agent, e.g. "
            "--agent-model architecture=claude-opus-5-5. Repeatable. "
            f"Agent names: {', '.join(config.AGENT_NAMES)}."
        ),
    )
    # Read what the user actually typed.
    args = parser.parse_args()

    # ---- 1b. List the knowledge bases and stop, if asked ----
    available = kb_loader.list_knowledge_bases()
    if args.list_kbs:
        print(f"Knowledge bases in {config.KNOWLEDGE_BASES_ROOT}:")
        for kb in available:
            print(f"  {kb.name}  ({kb.application_count} application(s))")
        if not available:
            print("  (none yet)")
        return 0
    if not args.requirement:
        parser.error("--requirement is required")
    check_scope(args.start_at, args.stop_after)

    # ---- 2. Apply the user's model choices ----
    if args.model:
        config.MODEL_ID = args.model
    agent_models = _parse_agent_model_overrides(args.agent_model)

    # ---- 3. Get the requirement text and give this run a unique ID ----
    requirement_text = _load_requirement(args.requirement)
    run_id = graph.new_run_id()

    # ---- 4. Print a header describing the run ----
    print("=" * 70)
    print("ARCHITECTURE-LEVEL SDLC")
    print("=" * 70)
    print(f"Run ID: {run_id}")
    print(f"Model:  {config.MODEL_ID} (default for every agent)")
    for agent, model in agent_models.items():
        print(f"        {agent}: {model}")

    # ---- 5. Choose the knowledge base: an existing one, or "something new".
    # It is optional: with none, the agents design from scratch. ----
    names = [kb.name for kb in available]
    if args.kb and args.new_kb:
        raise SystemExit("Choose either --kb NAME or --new-kb, not both.")
    if args.kb:
        if args.kb not in names:
            raise SystemExit(f"No knowledge base named {args.kb!r}. Available: {', '.join(names) or '(none)'}.")
        kb_name = args.kb
    elif args.new_kb or not names:
        kb_name = ""
    elif len(names) == 1:
        kb_name = names[0]
    else:
        raise SystemExit(
            f"Several knowledge bases exist ({', '.join(names)}). Choose one with --kb NAME, "
            f"or start something new with --new-kb."
        )

    applications = kb_loader.load_all(kb_loader.kb_path(kb_name)) if kb_name else []
    if kb_name:
        print(f"\nKnowledge base: {kb_name} ({len(applications)} application(s))")
        for app in applications:
            print(f"  - {app.name}")
    else:
        print("\nKnowledge base: none - creating something new (a new knowledge base is offered at the end)")

    print("\nRequirement:")
    print(requirement_text.strip())
    print(f"\nRuns: {args.start_at} -> {args.stop_after}"
          + ("  (Requirement agent skipped: the text above is used as the requirement)" if args.start_at == "design" else ""))
    if args.direction:
        print("Architectural direction for the Design agent:")
        print("  " + _load_requirement(args.direction).strip().replace("\n", "\n  "))

    # ---- 6. Build the pipeline and set its starting data ----
    pipeline = graph.build_graph()
    initial_state = {
        "run_id": run_id,
        "requirement_text": requirement_text,
        "applications": applications,
        # "Create something new" (or no knowledge base at all) = from scratch.
        "force_from_scratch": not kb_name,
        "kb_name": kb_name,
        "start_at": args.start_at,
        "stop_after": args.stop_after,
        "architecture_direction": _load_requirement(args.direction).strip() if args.direction else "",
        "jira_confirm": _ask_to_create_in_jira,
        "kb_confirm": _ask_to_save_knowledge_base,
        "confluence_confirm": _ask_to_publish_to_confluence,
        "iteration": 0,
        "history": [],
        "agent_models": agent_models,
    }

    print("\n" + "=" * 70)
    print("RUNNING PIPELINE")
    print("=" * 70)

    # ---- 7. Run the pipeline, showing progress after each stage ----
    # `final_state` accumulates each stage's results as they arrive.
    final_state: dict = {}
    try:
        for event in pipeline.stream(initial_state, stream_mode="updates"):
            node_name = next(iter(event))      # which stage just finished
            node_result = event[node_name]     # what that stage produced
            final_state.update(node_result)
            _print_node_update(node_name, node_result)
            if node_name == "requirement" and args.requirement_doc and not node_result.get("requirement_skipped"):
                _save_requirement_docs(run_id, node_result["requirement"], args.requirement_doc, kb_name)
            if node_name == "quality_planning" and args.test_plan:
                _save_test_plan(run_id, final_state.get("requirement") or {}, node_result["quality_plan"])
            if node_name == "design" and args.design_doc and node_result.get("design", {}).get("change_required"):
                _save_design_docs(run_id, final_state.get("requirement") or {}, node_result["design"], args.design_doc, kb_name)
    except Exception as e:
        # Something failed: say so, save whatever was completed, exit with error.
        print(f"\nPIPELINE ERROR: {e}", file=sys.stderr)
        run_dir = run_log.dump_run_state(run_id, requirement_text, final_state, error=str(e))
        print(f"Partial state dumped to: {run_dir}", file=sys.stderr)
        return 1

    # ---- 8. Print the final outcome and where files were saved ----
    print("\n" + "=" * 70)
    print("FINAL RESULT")
    print("=" * 70)
    print(f"Outcome:    {final_state.get('run_outcome')}")
    print(f"Design passes: {final_state.get('iteration', 0)}")

    run_dir = run_log.dump_run_state(run_id, requirement_text, final_state)
    print(f"Run log:    {run_dir / 'history.json'}")
    print(f"Event log:  {run_dir / 'events.jsonl'}")
    print(f"Blackboard: {run_dir / 'blackboard.json'}")

    # Only if the plan was approved and released do mock Jira files exist.
    release_result = final_state.get("release_result")
    if release_result:
        print(f"\nJira epic, stories, tasks and subtasks: {run_dir / 'jira_mock'}")

    return 0


# Run main() only when this file is executed directly (not when imported).
if __name__ == "__main__":
    raise SystemExit(main())
