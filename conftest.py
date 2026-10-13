"""Pytest setup: keep the tests' run folders (and saved sessions) out of the real runs/ folder."""

import pytest

import config


@pytest.fixture(autouse=True)
def _private_runs_folder(tmp_path):
    old = config.RUNS_ROOT
    config.RUNS_ROOT = tmp_path / "runs"
    try:
        yield
    finally:
        config.RUNS_ROOT = old
