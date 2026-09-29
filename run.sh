#!/usr/bin/env sh
# FaultFalcon: ./run.sh  (first run only: creates the Python environment, a few minutes of downloads)
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python
if ! "$PY" -c "import streamlit, torch, transformers, chromadb" >/dev/null 2>&1; then
  command -v uv >/dev/null 2>&1 || { echo "Install uv once: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
  echo "First run: creating the Python environment (one time) ..."
  [ -x "$PY" ] || uv venv --python 3.10 .venv
  uv pip install --python "$PY" -r requirements.txt
fi
exec "$PY" -m ff.launch "$@"
