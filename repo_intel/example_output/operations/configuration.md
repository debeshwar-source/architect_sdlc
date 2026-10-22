# Environment configuration

`[Verified]` Variables read via `os.environ`/`getenv`/`process.env`-style access. Defaults, required-ness and valid values are **not** extracted — check the referenced lines.

| Variable | Read sites | Locations |
|---|---|---|
| `APPDATA` | 1 | `desktop.py:L99` |
| `BLUEPRINT_PARENT_PID` | 1 | `streamlit_app.py:L66` |
| `BP_TEST_FROM_HOME` | 1 | `test_desktop.py:L243` |
| `BP_TEST_OVERRIDDEN` | 1 | `test_desktop.py:L243` |
| `CLAUDE_SDLC_ARCHITECT_HOME` | 1 | `config.py:L44` |
| `CLAUDE_SDLC_ARCHITECT_KB_ROOT` | 3 | `config.py:L94`, `test_desktop.py:L57`, `test_hive.py:L22` |
| `CLAUDE_SDLC_ARCHITECT_MEMORY_DIR` | 2 | `config.py:L108`, `test_hive.py:L20` |
| `CLAUDE_SDLC_ARCHITECT_MODEL_ID` | 1 | `config.py:L58` |
| `CLAUDE_SDLC_ARCHITECT_RUNS_ROOT` | 2 | `config.py:L99`, `test_hive.py:L21` |
| `CONFLUENCE_BASE_URL` | 1 | `test_design.py:L594` |
| `CONFLUENCE_EMAIL` | 1 | `test_design.py:L595` |
| `CONFLUENCE_SPACE_KEY` | 1 | `test_design.py:L597` |
| `NAME` | 1 | `config.py:L55` |
| `PARENT_PID_ENV` | 1 | `desktop.py:L212` |
| `STREAMLIT_SERVER_ADDRESS` | 1 | `test_desktop.py:L61` |
| `XDG_DATA_HOME` | 2 | `desktop.py:L100`, `test_desktop.py:L85` |

<!-- manual:begin notes -->
<!-- manual:end notes -->
