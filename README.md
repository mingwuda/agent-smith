# Desktop Agent 桌面 AI 智能体

> 一个 LangGraph + FastAPI 驱动的桌面 AI 智能体，支持文件操作、代码执行、网页搜索与抓取、Skills 插件扩展、会话持久化。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| 🤖 **AI Agent 核心** | LangGraph ReAct Agent，自主规划 + 执行任务 |
| 📁 **文件操作** | 读写文件、管理目录、搜索文件、工作区隔离 |
| 🐍 **代码执行** | 沙箱执行 Python 代码，适合数据分析与脚本测试 |
| 🌐 **网页搜索** | 使用 `web_search` 搜索网页，使用 `web_fetch` 抓取网页正文 |
| 💻 **系统信息** | 获取 OS、Python 版本、磁盘空间等信息 |
| 🧩 **Skills 插件** | 基于 `SKILL.md` 的热加载技能系统，支持触发词匹配 |
| 📊 **用量追踪** | 区分模型 Token 与工具调用次数，按会话/日/Provider/模型统计 |
| 💬 **会话管理** | 多会话持久化到 SQLite，切换/删除/自动命名，兼容旧 JSON 会话迁移 |
| ⚙️ **设置持久化** | API Key、模型、地址保存在配置文件，重启不丢失 |
| 🔐 **登录保护** | 内置登录页，未登录无法访问操作页面和 API |
| 🖥 **桌面 UI** | Markdown 渲染聊天界面，暗色侧边栏，实时状态面板 |

---

## 快速开始

### 前置条件

- Python 3.10+（推荐 3.13）
- 一个 LLM API Key（支持 OpenAI / DeepSeek / 通义千问 等兼容接口）

### 1. 克隆

```bash
git clone https://gitee.com/mingwuda/desktop-agent.git
cd desktop-agent
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 启动

```bash
cd agent_core
python main.py
```

浏览器打开 **http://127.0.0.1:8899/** 即可使用。

Windows 也可以在项目根目录直接双击或执行：

```cmd
start.cmd
```

它会自动创建 `.venv`、安装依赖并启动服务。

如果要排查 Windows 依赖安装问题，可以先执行一个全新的在线验证环境：

```cmd
packaging\windows\verify-venv.cmd
```

如需使用内网 PyPI 镜像，可在执行脚本前设置：

```cmd
set DESKTOP_AGENT_PIP_INDEX_URL=http://your-internal-pypi/simple/
set DESKTOP_AGENT_PIP_TRUSTED_HOST=your-internal-pypi-host
```

验证通过后，也可以直接用这个环境启动：

```cmd
packaging\windows\verify-venv.cmd run
```

首次公网部署时建议先配置登录密码，避免服务裸露：

```bash
export DESKTOP_AGENT_AUTH_USER=admin
export DESKTOP_AGENT_AUTH_PASSWORD='your-strong-password'
export DESKTOP_AGENT_AUTH_SECRET='replace-with-a-random-secret'
```

如果未显式设置密码，服务会在 `~/.desktop_agent/auth.json` 中自动生成一组本机登录凭据。

### 4. 配置 API Key

打开页面后，点击右上角 **⚙️ 设置**，选择模型厂商并填写对应配置：

| 字段 | 说明 | 示例 |
|------|------|------|
| 模型厂商 | 当前使用的模型服务商 | `OpenAI` / `DeepSeek` / `通义千问` / `自定义` |
| API Key | 你的 LLM API 密钥 | `sk-xxx...` |
| 模型名称 | 使用的模型 | `gpt-4o` / `deepseek-chat` / `qwen-plus` |
| API 地址 | OpenAI 兼容 API 地址（可选） | `https://api.deepseek.com` / 留空用 OpenAI |

内置支持 OpenAI、DeepSeek、通义千问；也可以新增多个自定义厂商，为每个厂商分别保存名称、API Key、模型和 API 地址。配置完成后，可以在顶部状态区的厂商/模型下拉框中快速切换。

保存后自动生效，无需重启。

设置中还可以调整 **最大推理步数**，用于控制 LangGraph ReAct Agent 单次任务的最大循环步数。复杂任务可以适当调大，默认值为 `60`。

### 5. 开始对话

在输入框发送消息，Agent 会自动调用工具完成任务。例如：

> 「帮我列出工作区文件」
>
> 「写一段 Python 代码计算斐波那契数列，然后运行它」
>
> 「告诉我系统信息」
>
> 「搜索一下今天的 AI 新闻」
>
> 「打开这个链接并总结正文：https://example.com/article」
>
> 「帮我写一份日报」

---

## Windows 一键运行包

在 Windows 机器上执行以下命令生成可分发包：

```cmd
packaging\windows\build.cmd
```

也可以用 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1
```

构建完成后会生成：

```text
dist\windows\DesktopAgent-Windows\
dist\windows\DesktopAgent-Windows.zip
```

把 `DesktopAgent-Windows.zip` 发给用户，用户解压后双击 **Start Desktop Agent.bat** 即可启动服务并自动打开浏览器。配置、会话和工作区默认保存在用户目录下：

```text
%USERPROFILE%\.desktop_agent
%USERPROFILE%\agent_workspace
```

---

## Linux / 远程部署

示例：部署到 `/opt/desktop-agent`，监听 `8080` 端口。

```bash
git clone https://gitee.com/mingwuda/desktop-agent.git /opt/desktop-agent
cd /opt/desktop-agent
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt
```

创建 systemd 服务：

```ini
# /etc/systemd/system/desktop-agent.service
[Unit]
Description=Desktop Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/desktop-agent
Environment=AGENT_HOST=0.0.0.0
Environment=AGENT_PORT=8080
Environment=AGENT_OPEN_BROWSER=0
Environment=DESKTOP_AGENT_AUTH_USER=admin
Environment=DESKTOP_AGENT_AUTH_PASSWORD=change-me
Environment=DESKTOP_AGENT_AUTH_SECRET=replace-with-random-secret
ExecStart=/opt/desktop-agent/.venv/bin/python /opt/desktop-agent/agent_core/main.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
systemctl daemon-reload
systemctl enable desktop-agent
systemctl restart desktop-agent
systemctl status desktop-agent
```

打开 `http://服务器IP:8080/`，登录后即可进入操作页面。

常用运维命令：

```bash
journalctl -u desktop-agent -f
systemctl restart desktop-agent
systemctl is-active desktop-agent
```

---

## 项目结构

```
desktop-agent/
├── agent_core/                    # Python 后端
│   ├── main.py                    # FastAPI 入口 + 所有 API 路由
│   ├── agent.py                   # DesktopAgent 核心（LangGraph）
│   ├── config.py                  # 配置管理（文件 + 环境变量）
│   ├── session_store.py           # 会话 SQLite 存储 + 旧 JSON 自动迁移
│   ├── tools/
│   │   ├── file_tools.py          # 文件操作（读写/列目录/搜索/删除）
│   │   ├── code_tools.py          # Python 代码执行
│   │   ├── system_tools.py        # 系统信息获取
│   │   └── web_tools.py           # 网页搜索与正文抓取
│   ├── skills/
│   │   ├── loader.py              # SKILL.md 解析器
│   │   └── registry.py            # 技能注册表（热加载/触发词匹配）
│   ├── monitoring/
│   │   └── usage_tracker.py       # Token 用量追踪
│   ├── memory/
│   │   └── local_memory.py        # 本地记忆存储
│   └── samples/daily-report/
│       └── SKILL.md               # 示例技能：日报生成
├── desktop/                       # 前端 UI
│   ├── index.html                 # 单页聊天应用
│   └── package.json
├── start.sh                       # 启动脚本
├── requirements.txt               # Python 依赖
└── .gitignore
```

---

## API 文档

启动后访问 **http://127.0.0.1:8899/docs** 可交互式测试所有接口。启用登录保护后，除 `/login`、`/auth/login`、`/auth/logout`、`/health` 外，其它页面和 API 都需要先登录。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 桌面 UI |
| `/login` | GET | 登录页 |
| `/auth/login` | POST | 登录并写入 HttpOnly Cookie |
| `/auth/logout` | POST | 退出登录 |
| `/run` | POST | 发送消息给 Agent |
| `/sessions` | GET | 列出所有会话 |
| `/sessions` | POST | 创建新会话 |
| `/sessions/{id}` | GET | 获取会话消息历史 |
| `/sessions/{id}` | DELETE | 删除会话 |
| `/sessions/{id}/rename` | PUT | 重命名会话 |
| `/skills` | GET | 列出已加载技能 |
| `/skills/reload` | POST | 热加载所有技能 |
| `/settings` | GET | 获取当前配置 |
| `/settings` | POST | 保存配置并重启 Agent |
| `/usage` | GET | 今日用量统计 |
| `/usage/session` | GET | 当前会话用量 |
| `/usage/history` | GET | 历史用量（支持 `?days=7`） |
| `/health` | GET | 健康检查 |

---

## Skills 插件开发

### 最小 Skill 结构

```
my-skill/
└── SKILL.md
```

### SKILL.md 示例

```markdown
# 日报生成助手

## Description
自动生成结构化日报，按项目分类今日工作

## Trigger
写日报、今日总结、生成日报、每日汇报

## Instructions
1. 询问用户今天完成了哪些工作
2. 按项目分类整理
3. 生成 markdown 格式的日报文件
4. 保存到工作区的 reports/ 目录

## Tools Required
file_write
```

将 Skill 目录放到 `agent_core/samples/` 下，然后调用 `/skills/reload` 即可热加载。

---

## 配置说明

配置文件路径：`~/.desktop_agent/config.json`

登录凭据文件路径：`~/.desktop_agent/auth.json`

会话数据库路径：`~/.desktop_agent/sessions.sqlite3`

旧版本的 `~/.desktop_agent/sessions/*.json` 会在首次访问会话存储时自动迁移到 SQLite，原 JSON 文件会保留作为备份。

支持通过环境变量覆盖：

| 环境变量 | 对应配置 | 默认值 |
|---------|---------|--------|
| `LLM_PROVIDER` | 当前模型厂商 | `openai` |
| `LLM_API_KEY` / `OPENAI_API_KEY` | API Key | - |
| `LLM_MODEL` | 模型名称 | `gpt-4o` |
| `LLM_BASE_URL` / `OPENAI_BASE_URL` | API 地址 | - |
| `AGENT_WORKSPACE` | 工作区目录 | `~/agent_workspace` |
| `AGENT_SKILLS_DIR` | Skills 目录 | 内置 samples |
| `AGENT_HOST` | 监听地址 | `127.0.0.1` |
| `AGENT_PORT` | 监听端口 | `8899` |
| `AGENT_RECURSION_LIMIT` | 最大推理步数 | `60` |
| `DESKTOP_AGENT_AUTH_USER` | 登录用户名 | `admin` |
| `DESKTOP_AGENT_AUTH_PASSWORD` | 登录密码 | 自动生成 |
| `DESKTOP_AGENT_AUTH_SECRET` | Cookie 签名密钥 | 自动生成 |
| `DESKTOP_AGENT_AUTH_COOKIE_SECURE` | Cookie 是否仅 HTTPS 发送 | `0` |

### 用量统计说明

用量记录保存在 `~/.desktop_agent/sessions.sqlite3` 的 `usage_records` 表中。旧版本的 `~/.desktop_agent/usage/YYYY-MM-DD.jsonl` 会在首次启动后自动迁移到 SQLite，原文件会保留作为备份。

- `model_calls`：模型调用次数
- `tool_calls`：工具调用次数
- `total_input_tokens` / `total_output_tokens`：只统计模型 Token
- `provider_breakdown`：按模型厂商聚合
- `model_breakdown`：按 `provider:model` 聚合
- `tool_breakdown`：按工具名统计调用次数

工具返回内容不会计入模型 Token。

---

## 技术栈

| 层面 | 技术 |
|------|------|
| Agent 框架 | **LangGraph** (ReAct Agent) |
| 大模型接口 | **LangChain OpenAI** (兼容 OpenAI / DeepSeek / 通义等) |
| Web 框架 | **FastAPI** + Uvicorn |
| 前端 | 原生 HTML/CSS/JS + **marked.js** (Markdown 渲染) |
| 记忆/状态 | LangGraph MemorySaver + SQLite 会话库 |
| Python 版本 | 3.10+（推荐 3.13） |

---

## 许可证

MIT
