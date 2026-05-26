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

AGENT_PORT="${AGENT_PORT:-8899}"

# 检查端口是否已被占用，是则杀死旧进程
if lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
  OLD_PIDS=$(lsof -ti ":$AGENT_PORT" 2>/dev/null | tr '\n' ' ')
  echo "⚠️  端口 $AGENT_PORT 已被占用 (PID: $OLD_PIDS)，正在关闭旧进程..."
  kill -9 $OLD_PIDS 2>/dev/null
  sleep 1
  # 等待端口释放
  for i in $(seq 1 5); do
    if ! lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  if lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
    echo "❌ 无法关闭旧进程 (PID: $(lsof -ti :$AGENT_PORT))，请手动处理"
    exit 1
  fi
  echo "✅ 旧进程已关闭"
  echo ""
fi

cd "$SCRIPT_DIR/agent_core"
echo "🚀 Agent 启动中... http://127.0.0.1:$AGENT_PORT"
exec "$PYTHON" main.py
