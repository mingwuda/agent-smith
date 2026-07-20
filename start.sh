#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 探测可用的 Python
PYTHON=""
for candidate in "$SCRIPT_DIR/.venv/bin/python" \
                 "/Users/mingo/.workbuddy/binaries/python/versions/3.13.12/bin/python3" \
                 "/usr/bin/python3" \
                 "python3"; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  # 尝试用系统 Python 创建虚拟环境
  SYS_PY=$(command -v python3 2>/dev/null || echo "/usr/bin/python3")
  if [[ -x "$SYS_PY" ]]; then
    echo "📦 正在创建 Python 虚拟环境..."
    "$SYS_PY" -m venv "$SCRIPT_DIR/.venv" 2>/dev/null
    if [[ -x "$SCRIPT_DIR/.venv/bin/pip" ]]; then
      echo "   安装依赖..."
      "$SCRIPT_DIR/.venv/bin/pip" install -q --timeout 120 \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null
      if "$SCRIPT_DIR/.venv/bin/python" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
        PYTHON="$SCRIPT_DIR/.venv/bin/python"
      fi
    fi
  fi
fi

if [[ -z "$PYTHON" ]]; then
  echo "Error: Python 3 with required packages was not found."
  echo ""
  echo "Try:"
  echo "  cd \"$SCRIPT_DIR\""
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/python -m pip install -r requirements.txt"
  echo "  ./start.sh"
  exit 1
fi

echo "Starting Moss Agent..."
echo "   Python: $("$PYTHON" --version)"
echo "   Path: $PYTHON"
echo ""

AGENT_PORT="${AGENT_PORT:-8899}"
export DESKTOP_AGENT_PORT="$AGENT_PORT"
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

# ── 单实例守卫：清理遗留的 main.py 工作进程 ──
# 背景：start.sh 以 nohup 后台拉起 python 时，若该进程被外层(systemd)误杀，
# python 子进程会脱离端口绑定继续后台运行(尤其微信轮询)，普通端口检测抓不到它，
# 导致"两个进程同时轮询同一账号→双回复"。这里在启动/重启前显式清理所有 main.py 工作进程。
kill_stray_workers() {
  local pids
  pids=$(pgrep -f "main\.py" 2>/dev/null | grep -v "^$$\$") || true
  if [[ -n "$pids" ]]; then
    echo "🧹 清理遗留的 Agent 工作进程: $pids"
    kill $pids 2>/dev/null || true
    sleep 1
    # 仍有残留则强制
    pids=$(pgrep -f "main\.py" 2>/dev/null | grep -v "^$$\$") || true
    [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
  fi
}

# ── 查看状态 ──
if [[ "$ACTION" == "status" ]]; then
  if [[ -f "$PIDFILE" ]]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "✅ Agent 正在运行 (PID: $OLD_PID)"
      echo "   地址: http://127.0.0.1:$AGENT_PORT"
      echo "   日志: $HOME/.desktop_agent/logs/agent.log"
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
    kill "$OLD_PID" 2>/dev/null && sleep 1 || true
    kill -0 "$OLD_PID" 2>/dev/null && kill -9 "$OLD_PID" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "✅ 已停止"
  else
    OLD_PID=$(lsof -ti ":$AGENT_PORT" 2>/dev/null)
    if [[ -n "$OLD_PID" ]]; then
      echo "🛑 停止 Agent (PID: $OLD_PID)..."
      kill "$OLD_PID" 2>/dev/null && sleep 1 || true
      kill -0 "$OLD_PID" 2>/dev/null && kill -9 "$OLD_PID" 2>/dev/null || true
      echo "✅ 已停止"
    else
      echo "❌ Agent 未运行"
    fi
  fi
  exit 0
fi

# ── 自动回退相关函数 ──
MAX_ROLLBACK="${MAX_ROLLBACK:-3}"
ROLLBACK_LOG="$SCRIPT_DIR/.rollback.log"

rollback_if_needed() {
  local new_pid=$1
  local commit_before=$2
  local rollback_count=$3
  local max_wait=${4:-15}

  echo "⏳ 等待健康检查通过 (最多 ${max_wait} 秒)..."
  local i
  for i in $(seq 1 "$max_wait"); do
    if curl -sf "http://127.0.0.1:$AGENT_PORT/health" >/dev/null 2>&1; then
      echo "✅ 健康检查通过，启动成功"
      return 0
    fi
    sleep 1
  done

  echo "❌ 健康检查失败，进程可能启动异常"
  if [[ -n "$commit_before" ]] && [[ "$rollback_count" -lt "$MAX_ROLLBACK" ]]; then
    echo "🔄 尝试自动回退到上一版本..."
    kill "$new_pid" 2>/dev/null || true
    sleep 1

    cd "$SCRIPT_DIR"
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      local prev_commit
      prev_commit=$(git rev-parse HEAD~1 2>/dev/null)
      if [[ -n "$prev_commit" ]]; then
        git revert --no-commit HEAD 2>/dev/null || true
        git commit -m "auto rollback: restart failed (attempt $((rollback_count + 1))/$MAX_ROLLBACK)" 2>/dev/null || true
        echo "   📋 已回退到: $(git log -1 --format='%h %s')"
        echo "$(date '+%Y-%m-%d %H:%M:%S') rollback to $prev_commit (attempt $((rollback_count + 1)))" >> "$ROLLBACK_LOG"
        cd "$SCRIPT_DIR/agent_core"
        # 递归回退
        rollback_once "$((rollback_count + 1))" "$max_wait"
        return $?
      fi
    fi
    echo "⚠️  无法执行回退（非 git 仓库或无父提交）"
  else
    echo "⚠️  已达到最大回退次数 ($MAX_ROLLBACK) 或无法回退"
  fi
  return 1
}

rollback_once() {
  local rollback_count=${1:-0}
  local max_wait=${2:-15}

  kill_stray_workers
  nohup "$PYTHON" main.py > "$SCRIPT_DIR/agent.log" 2>&1 &
  local new_pid=$!
  echo $! > "$PIDFILE"
  echo "🔄 已启动回退版本 (PID: $new_pid)"

  rollback_if_needed "$new_pid" "" "$rollback_count" "$max_wait"
}

# ── 重启 ──
if [[ "$ACTION" == "restart" ]]; then
  echo "🔄 重启 Agent..."
  local commit_before=""
  cd "$SCRIPT_DIR"
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    commit_before=$(git rev-parse HEAD 2>/dev/null)
    echo "   📋 当前版本: $(git log -1 --format='%h %s')"
  fi

  if [[ -f "$PIDFILE" ]]; then
    kill $(cat "$PIDFILE") 2>/dev/null || true; rm -f "$PIDFILE"
  fi
  OLD_PID=$(lsof -ti ":$AGENT_PORT" 2>/dev/null)
  if [[ -n "$OLD_PID" ]]; then
    kill $OLD_PID 2>/dev/null || true; sleep 2
    if lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
      kill -9 $OLD_PID 2>/dev/null || true; sleep 1
    fi
  fi
  echo "   旧进程已停止"
  kill_stray_workers
  cd "$SCRIPT_DIR/agent_core"
  nohup "$PYTHON" main.py > "$SCRIPT_DIR/agent.log" 2>&1 &
  local new_pid=$!
  echo $! > "$PIDFILE"
  echo "🔄 已启动新版本 (PID: $new_pid)"

  rollback_if_needed "$new_pid" "$commit_before" 0 15
  exit $?
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
  # 先尝试优雅关闭 (SIGTERM)，等待 2 秒再强制 (SIGKILL)
  kill $OLD_PIDS 2>/dev/null; sleep 2
  if lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
    kill -9 $OLD_PIDS 2>/dev/null; sleep 1
    for i in $(seq 1 5); do
      if ! lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then break; fi
      sleep 1
    done
  fi
  if lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
    # 备选方案：使用 fuser
    fuser -k -9 "$AGENT_PORT"/tcp 2>/dev/null; sleep 1
  fi
  if lsof -ti ":$AGENT_PORT" >/dev/null 2>&1; then
    echo "❌ 无法关闭旧进程，请手动处理: kill -9 $OLD_PIDS"; exit 1
  fi
  rm -f "$PIDFILE"
  echo "✅ 旧进程已关闭"; echo ""
fi

cd "$SCRIPT_DIR/agent_core"
kill_stray_workers
echo "🚀 Agent 启动中... http://$AGENT_HOST:$AGENT_PORT"
echo "   日志目录: $HOME/.desktop_agent/logs/ (7天自动滚动)"

if [[ "$DAEMON" -eq 1 ]]; then
  nohup "$PYTHON" main.py > "$SCRIPT_DIR/agent.log" 2>&1 &
  echo $! > "$PIDFILE"
  echo "✅ Agent 已在后台启动 (PID: $(cat $PIDFILE))"
  echo "   日志: $SCRIPT_DIR/agent.log (shell 输出)"
  echo "   结构化日志: $HOME/.desktop_agent/logs/agent.log (7天滚动)"
  echo "   地址: http://$AGENT_HOST:$AGENT_PORT"
  echo ""
  echo "   管理命令:"
  echo "     ./start.sh status    查看状态"
  echo "     ./start.sh stop      停止"
  echo "     ./start.sh restart   重启"
else
  exec "$PYTHON" main.py
fi
