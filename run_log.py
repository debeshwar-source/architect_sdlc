"""Shared run-log dumping, used by both cli.py and streamlit_app.py."""

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
    run_dir = config.RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "run_id": run_id,
        "requirement_text": requirement_text,
        "run_outcome": final_state.get("run_outcome"),
        "error": error,
        "history": final_state.get("history", []),
    }
    (run_dir / "history.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    return run_dir
