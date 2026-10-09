"""
CLI entrypoint for the architecture-level SDLC orchestrator.

Takes a requirement that spans multiple applications, reasons over the
pre-loaded knowledge base of applications, and produces a cross-repo
implementation plan plus a Jira epic + one ticket per affected
application.

Usage:
    python3 cli.py --requirement "Notify users by email/push when a \\
        new episode of a show they're watching becomes available,
        respecting their existing notification preferences."

    python3 cli.py --requirement @requirement.txt

This is a DEMO: the knowledge_base/applications/*.md files describe a
fictional OTT streaming platform, not a real one.
"""

import argparse
import sys
from pathlib import Path

import config
import knowledge_base.loader as kb_loader
import orchestrator.graph as graph
import run_log


def _load_requirement(arg: str) -> str:
    if arg.startswith("@"):
        return Path(arg[1:]).read_text(encoding="utf-8")
    return arg


def _parse_agent_model_overrides(pairs: list) -> dict:
    overrides = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--agent-model expects AGENT=MODEL, got: {pair!r}")
        agent, model = (part.strip() for part in pair.split("=", 1))
        if agent not in config.AGENT_NAMES:
            raise SystemExit(
                f"--agent-model: unknown agent {agent!r} -- choices: "
                f"{', '.join(config.AGENT_NAMES)}"
            )
        if not model:
            raise SystemExit(f"--agent-model {agent}=... needs a model id.")
        overrides[agent] = model
    return overrides


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
    print(f"\n>>> {node_name}")

    if node_name == "requirement":
        req = node_result.get("requirement", {})
        print(f"    title: {req.get('title', '')}")
        _print_clarifying_questions(req)

    elif node_name == "architecture":
        arch = node_result.get("architecture", {})
        print(f"    change_required: {arch.get('change_required')}")
        affected = arch.get("affected_applications", [])
        if affected:
            print(f"    affected: {[a['name'] for a in affected]}")
        new_apps = arch.get("new_applications_needed", [])
        if new_apps:
            print(f"    new applications proposed: {[a['name'] for a in new_apps]}")
        if arch.get("dependency_order"):
            print(f"    dependency order: {arch['dependency_order']}")
        _print_clarifying_questions(arch)

    elif node_name == "quality":
        quality = node_result.get("quality", {})
        print(f"    scenarios: {len(quality.get('per_application_test_scenarios', []))}")
        print(f"    contract tests: {len(quality.get('contract_tests', []))}")
        _print_clarifying_questions(quality)

    elif node_name == "implementation_spec":
        spec = node_result.get("implementation_spec", {})
        print(f"    specs produced: {[s['application'] for s in spec.get('specs', [])]}")
        _print_clarifying_questions(spec)

    elif node_name == "review":
        review = node_result.get("review", {})
        findings = review.get("findings", [])
        blocking = sum(1 for f in findings if f.get("severity") == "BLOCKING")
        followup = len(findings) - blocking
        print(f"    status: {review.get('status')} ({blocking} blocking, {followup} follow-up)")
        brk = node_result.get("breaker") or {}
        if brk.get("level") not in (None, "ok"):
            print(f"    circuit breaker: {brk['level']} -- {brk.get('reason')}")
        _print_clarifying_questions(review)

    elif node_name == "release_planning":
        plan = node_result.get("release_plan", {})
        print(f"    epic: {plan.get('epic', {}).get('title', '')}")
        print(f"    tickets planned: {len(plan.get('tickets', []))}")
        print(f"    rollout stages: {len(plan.get('rollout_stages', []))}")
        _print_clarifying_questions(plan)

    elif node_name == "release_apply":
        result = node_result.get("release_result", {})
        print(f"    epic: {result.get('epic_id')} -> {result.get('epic_path')}")
        for ticket in result.get("tickets", []):
            print(f"    ticket: {ticket['ticket_id']} ({ticket['application']})")

    elif node_name == "finalize":
        print(f"    outcome: {node_result.get('run_outcome')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Architecture-level SDLC orchestrator")
    parser.add_argument(
        "--requirement", required=True, help="Requirement text, or @path/to/file"
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
    args = parser.parse_args()

    if args.model:
        config.MODEL_ID = args.model
    agent_models = _parse_agent_model_overrides(args.agent_model)

    requirement_text = _load_requirement(args.requirement)
    run_id = graph.new_run_id()

    print("=" * 70)
    print("ARCHITECTURE-LEVEL SDLC")
    print("=" * 70)
    print(f"Run ID: {run_id}")
    print(f"Model:  {config.MODEL_ID} (default for every agent)")
    for agent, model in agent_models.items():
        print(f"        {agent}: {model}")

    applications = kb_loader.load_all()
    if not applications:
        print(f"FAILED: no knowledge base found at {config.KNOWLEDGE_BASE_DIR}", file=sys.stderr)
        return 1

    print(f"\nKnowledge base: {len(applications)} application(s) loaded")
    for app in applications:
        print(f"  - {app.name}")

    print("\nRequirement:")
    print(requirement_text.strip())

    pipeline = graph.build_graph()
    initial_state = {
        "run_id": run_id,
        "requirement_text": requirement_text,
        "applications": applications,
        "iteration": 0,
        "history": [],
        "agent_models": agent_models,
    }

    print("\n" + "=" * 70)
    print("RUNNING PIPELINE")
    print("=" * 70)

    final_state: dict = {}
    try:
        for event in pipeline.stream(initial_state, stream_mode="updates"):
            node_name = next(iter(event))
            node_result = event[node_name]
            final_state.update(node_result)
            _print_node_update(node_name, node_result)
    except Exception as e:
        print(f"\nPIPELINE ERROR: {e}", file=sys.stderr)
        run_dir = run_log.dump_run_state(run_id, requirement_text, final_state, error=str(e))
        print(f"Partial state dumped to: {run_dir}", file=sys.stderr)
        return 1

    print("\n" + "=" * 70)
    print("FINAL RESULT")
    print("=" * 70)
    print(f"Outcome:    {final_state.get('run_outcome')}")
    print(f"Iterations: {final_state.get('iteration', 0)}")

    run_dir = run_log.dump_run_state(run_id, requirement_text, final_state)
    print(f"Run log:    {run_dir / 'history.json'}")
    print(f"Event log:  {run_dir / 'events.jsonl'}")
    print(f"Blackboard: {run_dir / 'blackboard.json'}")

    release_result = final_state.get("release_result")
    if release_result:
        print(f"\nJira epic + tickets: {run_dir / 'jira_mock'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
