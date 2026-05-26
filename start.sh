#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 优先使用项目虚拟环境
if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
  USING_VENV=1
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
  USING_VENV=0
else
  echo "Error: Python 3 was not found."
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
PIDFILE="$SCRIPT_DIR/.agent.pid"
DAEMON=0
ACTION="start"

# 解析参数（必须在任何操作之前）
for arg in "$@"; do
  case "$arg" in
    -d|--daemon|--background) DAEMON=1 ;;
    --stop|stop)              ACTION="stop" ;;
    --restart|restart)        ACTION="restart" ;;
    --status|status)          ACTION="status" ;;
  esac
done

# ── 查看状态 ──
if [[ "$ACTION" == "status" ]]; then
  if [[ -f "$PIDFILE" ]]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "✅ Agent 正在运行 (PID: $OLD_PID)"
      echo "   http://127.0.0.1:$AGENT_PORT"
    else
      echo "❌ PID 文件存在但进程已不存在"
      rm -f "$PIDFILE"
    fi
  else
    if lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
      echo "⚠️  Agent 正在运行但无 PID 文件（非后台模式启动）"
      echo "   PID: $(lsof -ti :$AGENT_PORT)"
    else
      echo "❌ Agent 未运行"
    fi
  fi
  exit 0
fi

# ── 停止 ──
if [[ "$ACTION" == "stop" ]]; then
  if [[ -f "$PIDFILE" ]]; then
    OLD_PID=$(cat "$PIDFILE")
    echo "🛑 停止 Agent (PID: $OLD_PID)..."
    kill "$OLD_PID" 2>/dev/null && echo "✅ 已停止" || echo "⚠️ 停止失败"
    rm -f "$PIDFILE"
  else
    OLD_PID=$(lsof -ti ":$AGENT_PORT" 2>/dev/null)
    if [[ -n "$OLD_PID" ]]; then
      echo "🛑 停止 Agent (PID: $OLD_PID)..."
      kill "$OLD_PID" 2>/dev/null && echo "✅ 已停止"
    else
      echo "❌ Agent 未运行"
    fi
  fi
  exit 0
fi

# ── 重启 ──
if [[ "$ACTION" == "restart" ]]; then
  echo "🔄 重启 Agent..."
  if [[ -f "$PIDFILE" ]]; then
    kill $(cat "$PIDFILE") 2>/dev/null || true; rm -f "$PIDFILE"
  fi
  OLD_PID=$(lsof -ti ":$AGENT_PORT" 2>/dev/null)
  if [[ -n "$OLD_PID" ]]; then kill $OLD_PID 2>/dev/null || true; sleep 1; fi
  echo "   旧进程已停止"
  cd "$SCRIPT_DIR/agent_core"
  nohup "$PYTHON" main.py > "$SCRIPT_DIR/agent.log" 2>&1 &
  echo $! > "$PIDFILE"
  echo "✅ Agent 已重启 (PID: $(cat $PIDFILE))"
  exit 0
fi

# ── 以下为启动逻辑（start） ──

# 是否开放外部访问
if [[ "$*" == *"--public"* ]] || [[ "${PUBLIC:-}" == "1" ]]; then
  AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
  echo "🌐 开放模式 — 局域网内其他设备可通过以下地址访问："
  LOCAL_IP=$(ifconfig 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | head -1)
  [[ -n "$LOCAL_IP" ]] && echo "      http://$LOCAL_IP:$AGENT_PORT"
  echo "      http://<本机局域网IP>:$AGENT_PORT"
  echo ""
else
  AGENT_HOST="${AGENT_HOST:-127.0.0.1}"
fi
export AGENT_HOST

# 检查端口是否已被占用，是则杀死旧进程
if lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
  OLD_PIDS=$(lsof -ti ":$AGENT_PORT" 2>/dev/null | tr '\n' ' ')
  echo "⚠️  端口 $AGENT_PORT 已被占用 (PID: $OLD_PIDS)，正在关闭旧进程..."
  kill -9 $OLD_PIDS 2>/dev/null; sleep 1
  for i in $(seq 1 5); do
    if ! lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  if lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
    echo "❌ 无法关闭旧进程，请手动处理"; exit 1
  fi
  echo "✅ 旧进程已关闭"; echo ""
fi

cd "$SCRIPT_DIR/agent_core"
echo "🚀 Agent 启动中... http://$AGENT_HOST:$AGENT_PORT"

if [[ "$DAEMON" -eq 1 ]]; then
  nohup "$PYTHON" main.py > "$SCRIPT_DIR/agent.log" 2>&1 &
  echo $! > "$PIDFILE"
  echo "✅ Agent 已在后台启动 (PID: $(cat $PIDFILE))"
  echo "   日志: $SCRIPT_DIR/agent.log"
  echo "   地址: http://$AGENT_HOST:$AGENT_PORT"
  echo ""
  echo "   管理命令:"
  echo "     ./start.sh status    查看状态"
  echo "     ./start.sh stop      停止"
  echo "     ./start.sh restart   重启"
else
  exec "$PYTHON" main.py
fi
