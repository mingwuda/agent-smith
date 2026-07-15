#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 升级项目 venv 的 Python:3.9.6 -> 3.13.12(官方 MCP SDK 需要 >=3.10)
# 安全策略:先备份旧 venv,升级失败自动回滚,绝不留下半残状态。
# 用法:  bash upgrade_python_313.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLD_VENV="$SCRIPT_DIR/.venv"
BAK_VENV="$SCRIPT_DIR/.venv_py39_bak"
NEW_PY="/Users/mingo/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
TUNA_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

# 关键:剥离所有代理环境变量后再跑 pip。
# 原因:环境里 ALL_PROXY=socks5://... 会污染 pip 的「隔离构建子进程」——该子进程
# 不继承 venv 里的 socksio,一旦某包需源码编译、构建后端用 httpx 联网就会
# ImportError(socksio not installed)而失败。清华镜像本身直连可达,无需代理。
# --prefer-binary:优先预编译 wheel,避免 cryptography 等触发 Rust 源码编译。
NOPROXY=(env -u ALL_PROXY -u all_proxy -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy -u NO_PROXY -u no_proxy)
pip_install() {
  "${NOPROXY[@]}" "$PIP" install --prefer-binary -i "$TUNA_MIRROR" "$@"
}

rollback() {
  echo "↩️  回滚:恢复旧 venv"
  rm -rf "$OLD_VENV"
  if [[ -d "$BAK_VENV" ]]; then
    mv "$BAK_VENV" "$OLD_VENV"
    echo "   已恢复 -> $OLD_VENV (Python $("$OLD_VENV/bin/python" --version 2>&1))"
  fi
}

echo "=== Python 升级 (3.9.6 -> 3.13.12) ==="
echo "项目目录: $SCRIPT_DIR"

# 0. 前置检查
if [[ ! -x "$NEW_PY" ]]; then
  echo "❌ 找不到托管 Python: $NEW_PY"
  echo "   请确认路径存在,或把 NEW_PY 改成任意 3.10+ 解释器绝对路径。"
  exit 1
fi
echo "目标 Python: $("$NEW_PY" --version 2>&1)"

# 1. 备份旧 venv
# 重跑保护:若已存在 3.9 备份(上次失败留下的),说明当前 .venv 可能是半残的 3.13,
# 此时绝不能用它覆盖真备份——直接丢弃当前 .venv,保留原备份。
if [[ -d "$BAK_VENV" ]]; then
  echo "ℹ️  检测到已有备份 $BAK_VENV(上次运行遗留),保留它;丢弃当前 .venv 后重建"
  rm -rf "$OLD_VENV"
elif [[ -d "$OLD_VENV" ]]; then
  echo "📦 备份旧 venv -> $BAK_VENV"
  mv "$OLD_VENV" "$BAK_VENV"
fi

# 2. 创建新 venv
echo "🆕 用 3.13.12 创建新 venv..."
if ! "$NEW_PY" -m venv "$OLD_VENV"; then
  echo "❌ 创建 venv 失败"
  rollback; exit 1
fi
PIP="$OLD_VENV/bin/pip"
echo "⬆️  升级 pip/setuptools/wheel(无代理 + 清华源)..."
pip_install --upgrade pip setuptools wheel

# 3. 安装依赖(无代理 + 清华镜像 + 优先 wheel)
echo "📥 安装 requirements.txt ..."
if ! pip_install -r "$SCRIPT_DIR/requirements.txt"; then
  echo "❌ 依赖安装失败"
  rollback; exit 1
fi

# 4. 安装官方 MCP SDK(本次升级要达成的前提)
echo "📥 安装官方 mcp SDK (>=1.27,<2) ..."
if ! pip_install 'mcp>=1.27,<2'; then
  echo "❌ mcp SDK 安装失败"
  rollback; exit 1
fi

# 5. 冒烟测试:关键依赖可导入 + 核心模块可编译
echo "🔍 冒烟测试..."
if ! "$OLD_VENV/bin/python" - <<'PY'
import sys
print("python:", sys.version.split()[0])
import langgraph, langchain_openai, fastapi, uvicorn
print("langgraph/langchain_openai/fastapi/uvicorn OK")
import mcp
print("mcp SDK OK:", getattr(mcp, "__version__", "?"))
import py_compile
for f in ["agent_core/agent.py", "agent_core/main.py", "agent_core/tools/mcp_tools.py"]:
    py_compile.compile(f, doraise=True)
print("py_compile OK: agent / main / mcp_tools")
PY
then
  echo "❌ 冒烟测试失败"
  rollback; exit 1
fi

# 6. 把 mcp 写回 requirements.txt(让 venv 可复现)
if ! grep -q '^mcp>=' "$SCRIPT_DIR/requirements.txt"; then
  printf 'mcp>=1.27,<2  # 官方 MCP SDK(stdio/SSE/HTTP 传输,支持协议版本协商)\n' >> "$SCRIPT_DIR/requirements.txt"
  echo "📝 已把 mcp 加入 requirements.txt"
fi

echo ""
echo "✅ 升级成功!venv 现在使用 $("$OLD_VENV/bin/python" --version 2>&1)"
echo "   旧 venv 备份在 $BAK_VENV(确认无误后可手动删除)"
echo "   如需回滚: rm -rf $OLD_VENV && mv $BAK_VENV $OLD_VENV"
echo "   注意:playwright 浏览器需另行 'playwright install'(仅浏览器类工具用到)"
