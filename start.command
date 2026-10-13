#!/bin/bash
# Double-click in Finder to run lightlaland. The first run sets up a Python environment.
cd "$(dirname "$0")" || exit 1
if [ ! -d .venv ]; then
  echo "First run: setting up Python packages..."
  python3 -m venv .venv || { echo "Install Python 3 first (python.org or Homebrew)."; read -r; exit 1; }
  .venv/bin/pip install -q -r requirements.txt || { echo "pip install failed"; read -r; exit 1; }
fi
exec .venv/bin/python -m lightlaland "$@"
