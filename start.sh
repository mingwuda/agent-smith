#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
  USING_VENV=1
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
  USING_VENV=0
else
  echo "Error: Python 3 was not found."
  echo "Install Python 3.10+ or create a virtual environment at: $SCRIPT_DIR/.venv"
  exit 1
fi

if ! "$PYTHON" -c 'import fastapi, uvicorn, langchain_openai, langgraph' >/dev/null 2>&1; then
  echo "Error: required Python packages are missing."
  if [[ "$USING_VENV" -eq 1 ]]; then
    echo "Run: \"$PYTHON\" -m pip install -r \"$SCRIPT_DIR/requirements.txt\""
  else
    echo "Create a project virtual environment, then install dependencies:"
    echo "  cd \"$SCRIPT_DIR\""
    echo "  \"$PYTHON\" -m venv .venv"
    echo "  .venv/bin/python -m pip install -r requirements.txt"
    echo "  ./start.sh"
  fi
  exit 1
fi

echo "Starting Desktop Agent..."
echo "   Python: $("$PYTHON" --version)"
echo "   Path: $PYTHON"
echo ""

cd "$SCRIPT_DIR/agent_core"
exec "$PYTHON" main.py
