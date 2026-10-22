#!/usr/bin/env bash
# Builds the double-clickable desktop app (dist/Blueprint.app on a Mac).
#
#   ./packaging/build_desktop.sh            build, then self-test the result
#
# Needs the packages in requirements-build.txt. If a
# virtual environment exists at .venv-desktop it is used, otherwise python3 is.
# The build bundles Python, Streamlit, the pipeline and the Claude Code CLI that
# ships with claude-agent-sdk, so expect a large app (several hundred MB).
set -euo pipefail
cd "$(dirname "$0")/.."

PY=python3
[ -x .venv-desktop/bin/python ] && PY=.venv-desktop/bin/python

if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "PyInstaller is not installed. Run:  $PY -m pip install -r requirements-build.txt" >&2
  exit 1
fi

"$PY" -m PyInstaller --noconfirm --clean --windowed --name Blueprint \
  --specpath build/spec \
  --osx-bundle-identifier com.blueprint.sdlc \
  --collect-all streamlit --copy-metadata streamlit \
  --collect-all claude_agent_sdk --copy-metadata claude-agent-sdk \
  --collect-all langgraph --collect-all langgraph_checkpoint --collect-all langchain_core \
  --collect-all docx --collect-all reportlab --collect-all openpyxl \
  --collect-submodules agents --collect-submodules hive --collect-submodules tools \
  --collect-submodules orchestrator --collect-submodules knowledge_base --collect-submodules coding --collect-submodules review --collect-submodules qa \
  --add-data "$PWD/streamlit_app.py:." \
  --add-data "$PWD/.streamlit:.streamlit" \
  --add-data "$PWD/.env.example:." \
  --add-data "$PWD/skills:skills" \
  --add-data "$PWD/knowledge_base/streamly.md:knowledge_base" \
  desktop.py

APP=dist/Blueprint.app
[ -d "$APP" ] || APP=dist/Blueprint
echo
echo "Built: $APP"
echo "Checking it (no window, no AI call)..."
if [ -x "$APP/Contents/MacOS/Blueprint" ]; then
  "$APP/Contents/MacOS/Blueprint" --self-test
else
  "$APP/Blueprint" --self-test
fi
