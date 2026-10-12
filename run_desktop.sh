#!/usr/bin/env bash
# Run Blueprint in its own desktop window. One command, no setup:
#
#     ./run_desktop.sh
#
# The first time, it creates a private Python environment in .venv-desktop and
# installs what the app needs (a few minutes, needs internet). After that it just
# starts. It re-installs only if requirements.txt or requirements-desktop.txt change.
# Anything you add is passed on, e.g.  ./run_desktop.sh --self-test
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv-desktop

# Python 3.10 or newer is needed.
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Blueprint needs Python 3.10 or newer, and python3 was not found (or is too old)." >&2
  echo "Install it from https://www.python.org/downloads/ (or: brew install python), then run this again." >&2
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "First run: creating the environment in $VENV ..."
  python3 -m venv "$VENV"
fi

# Install (or update) the packages, but only when the requirement lists changed.
STAMP="$VENV/.installed-$(cat requirements.txt requirements-desktop.txt | shasum | cut -c1-12)"
if [ ! -f "$STAMP" ]; then
  echo "Installing the app's packages (this can take a few minutes the first time) ..."
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt -r requirements-desktop.txt
  rm -f "$VENV"/.installed-*
  touch "$STAMP"
fi

exec "$VENV/bin/python" desktop.py "$@"
