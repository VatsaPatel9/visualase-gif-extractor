#!/usr/bin/env bash
# Start the Visualase GIF Extractor on http://127.0.0.1:8790 (creates a local Python environment the first time).
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating a Python environment and installing requirements..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi
exec .venv/bin/python app.py "$@"
