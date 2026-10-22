"""Checks of the knowledge-base web page (repo_intel_app.py), driven with Streamlit's test harness."""

import subprocess
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).parent / "repo_intel_app.py")


def _repo(tmp_path):
    r = tmp_path / "r"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "a.py").write_text('"""Thing."""\ndef f():\n    pass\n')
    (r / "README.md").write_text("# R\n\nR is a small demo repository used to check the page works end to end.\n")
    for a in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)
    return r


def test_build_button_disabled_without_source():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception and at.button[0].disabled


def test_build_then_browse(tmp_path):
    at = AppTest.from_file(APP, default_timeout=90).run()
    at.text_input[0].set_value(str(_repo(tmp_path)))
    at.text_input[2].set_value(str(tmp_path / "lib"))
    depth = next(r for r in at.radio if r.options[0].startswith("Standard"))
    depth.set_value(depth.options[1])                 # 'Facts only': no real LLM calls in tests
    at = at.run()
    at = at.button[0].click().run()
    assert not at.exception, at.exception
    assert any("Status: ok" in s.value for s in at.success)
    assert (tmp_path / "lib" / "r" / "README.md").exists()
    assert any("README.md" in r.options for r in at.radio)
    assert any(r.options[0].startswith("Standard") for r in at.radio)       # Standard (1 AI call) is the default depth
    at = at.run()    # same repository again: same folder, "update" wording
    assert any("update" in c.value for c in at.caption)


def test_bad_source_shows_error(tmp_path):
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.text_input[0].set_value(str(tmp_path / "nope"))
    at.text_input[2].set_value(str(tmp_path / "lib"))
    at = at.run()
    at = at.button[0].click().run()
    assert not at.exception and at.error
