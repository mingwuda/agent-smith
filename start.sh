#!/bin/bash
# 启动桌面智能体 - 使用 managed Python 3.13
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/Users/mingo/.workbuddy/binaries/python/envs/desktop-agent/bin/python3"

echo "🚀 启动桌面 AI 智能体..."
echo "   Python: $($PYTHON --version)"
echo ""

cd "$SCRIPT_DIR/agent_core"
exec $PYTHON main.py
