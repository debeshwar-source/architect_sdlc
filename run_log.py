"""Shared run-log dumping, used by both cli.py and streamlit_app.py.

Plain words: after a run ends (successfully or not), this saves a
complete record of what happened to a file, so you can read it later.
"""

import json
from pathlib import Path
from typing import Optional

import config


def dump_run_state(
    run_id: str,
    requirement_text: str,
    final_state: dict,
    error: Optional[str] = None,
) -> Path:
    """Write the run's summary to runs/<run_id>/history.json and return
    the folder it was written to."""
    # Build the folder for this run (and any missing parent folders).
    # exist_ok=True means "don't complain if it's already there".
    run_dir = config.RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # The information we want to keep: who/what/how it ended, plus the
    # step-by-step history of every stage that ran.
    payload = {
        "run_id": run_id,
        "requirement_text": requirement_text,
        "run_outcome": final_state.get("run_outcome"),
        "error": error,
        "history": final_state.get("history", []),
    }
    # Save it as a human-readable (indented) JSON file. default=str makes
    # sure unusual values (like dates) are turned into text instead of
    # causing a crash.
    (run_dir / "history.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    return run_dir
