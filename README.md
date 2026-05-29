# Desktop Agent 桌面 AI 智能体

> 一个 LangGraph + FastAPI 驱动的桌面 AI 智能体，支持文件操作、代码执行、网页搜索与抓取、Skills 插件扩展、会话持久化。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| 🤖 **AI Agent 核心** | LangGraph ReAct Agent，自主规划 + 执行任务 |
| 📁 **文件操作** | 读写文件、管理目录、搜索文件、工作区隔离 |
| 🐍 **代码执行** | 沙箱执行 Python 代码，适合数据分析与脚本测试 |
| 🌿 **Git 操作** | 查看仓库状态、diff、提交日志和指定 revision 内容；用户明确要求时可暂存并创建本地提交 |
| 🌐 **网页搜索** | 使用 `web_search` 搜索网页，使用 `web_fetch` 抓取网页正文 |
| 💻 **系统信息** | 获取 OS、Python 版本、磁盘空间等信息 |
| 🧑‍💻 **子代理** | 通过 `delegate_task` 将独立任务委派给 coder / reviewer / debugger 子代理，同步执行并预留并行任务状态模型 |
| 🧩 **Skills 插件** | 基于 `SKILL.md` 的热加载技能系统，支持 YAML frontmatter、触发词匹配和项目内 `.opencode/skills` |
| 📊 **用量追踪** | 区分模型 Token 与工具调用次数，按会话/日/Provider/模型统计 |
| 💬 **会话管理** | 多会话按用户持久化到 SQLite，切换/删除/自动命名 |
| ⚙️ **设置持久化** | API Key、模型、地址保存在配置文件，重启不丢失 |
| 🔐 **登录保护** | 内置多用户登录页，未登录无法访问操作页面和 API，支持短期 URL Token 免密登录 |
| 🧠 **长期记忆** | 按用户隔离保存长期偏好、项目事实和常用环境信息，可在页面中管理 |
| 🖥 **桌面 UI** | Markdown 渲染聊天界面，暗色侧边栏，实时状态面板，支持流式回复、工具步骤展示、技能列表滚动和悬浮描述 |

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
./start.sh
```

浏览器打开 **http://127.0.0.1:8899/** 即可使用。

也可以手动从后端入口启动：

```bash
cd agent_core
python main.py
```

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

启动过一次服务后，可以生成短期免密登录链接，适合本机或可信内网临时访问：

```bash
python generate_login_url.py --host 127.0.0.1 --port 8899 --expires 300 --user admin
```

可选参数：

- `--qr`：在终端输出二维码（需要安装 `qrcode`）
- `--copy`：复制链接到剪贴板（需要安装 `pyperclip`）

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

模型 API 请求默认会对连接错误进行重试，避免网络短抖动时立即失败。默认最多重试 `3` 次，请求超时为 `30` 秒；可通过环境变量调整：

```bash
export AGENT_API_MAX_RETRIES=3
export AGENT_API_TIMEOUT_SECONDS=30
```

如果某些自定义模型网关的 DNS 在 Python 运行时里不稳定，可以通过 `AGENT_API_HOST_IPS` 指定兜底 IP，多个 IP 用英文逗号分隔。服务默认不会内置任何厂商 IP；只有显式配置后，才会在正常 DNS 失败时轮换这些 IP。这个选项主要用于运维排障，不建议普通部署默认配置。

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
> 「看一下这个仓库当前有哪些改动」
>
> 「把当前改动提交一下，提交信息是：完善 Skills 支持」
>
> 「帮我写一份日报」
>
> 「记住：我希望回复默认使用中文」

Git 工具提供 `git_status`、`git_diff`、`git_log`、`git_show`、`git_add`、`git_commit`、`git_commit_all` 和受限的 `git_command`。只有用户明确要求提交代码时，Agent 才应暂存并创建本地 commit；默认不开放 `pull`、`push`、`reset`、`restore` 等高风险操作。

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
│   ├── subagents.py               # 子代理运行时（同步 MVP + 任务状态模型）
│   ├── config.py                  # 配置管理（文件 + 环境变量）
│   ├── session_store.py           # 会话 SQLite 存储 + 旧 JSON 自动迁移
│   ├── memory/
│   │   └── local_memory.py        # 本地长期记忆存储
│   ├── tools/
│   │   ├── file_tools.py          # 文件操作（读写/列目录/搜索/删除）
│   │   ├── code_tools.py          # Python 代码执行
│   │   ├── git_tools.py           # Git 工具（status/diff/log/show/add/commit 等）
│   │   ├── system_tools.py        # 系统信息获取
│   │   ├── memory_tools.py        # 长期记忆工具（显式记忆/查询/删除）
│   │   └── web_tools.py           # 网页搜索与正文抓取
│   ├── skills/
│   │   ├── loader.py              # SKILL.md 解析器
│   │   └── registry.py            # 技能注册表（热加载/触发词匹配）
│   ├── monitoring/
│   │   └── usage_tracker.py       # Token 用量追踪
│   └── samples/daily-report/
│       └── SKILL.md               # 示例技能：日报生成
├── .opencode/skills/              # 项目内 Skills（兼容 oh-my-openagent / Superpowers 风格）
│   ├── frontend-ui-ux/
│   ├── test-driven-development/
│   ├── systematic-debugging/
│   ├── verification-before-completion/
│   ├── brainstorming/
│   ├── writing-plans/
│   ├── executing-plans/
│   └── receiving-code-review/
├── desktop/                       # 前端 UI
│   ├── index.html                 # 单页聊天应用
│   └── package.json
├── start.sh                       # 启动脚本
├── start.cmd                      # Windows 启动脚本
├── generate_login_url.py          # 生成短期 URL Token 登录链接
├── requirements.txt               # Python 依赖
└── .gitignore
```

---

## API 文档

启动后访问 **http://127.0.0.1:8899/docs** 可交互式测试所有接口。启用登录保护后，除 `/login`、`/auth/login`、`/auth/logout`、`/auth/token-login`、`/health` 外，其它页面和 API 都需要先登录。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 桌面 UI |
| `/login` | GET | 登录页 |
| `/auth/login` | POST | 登录并写入 HttpOnly Cookie |
| `/auth/logout` | POST | 退出登录 |
| `/auth/token-login` | GET | 使用短期 URL Token 登录 |
| `/run` | POST | 发送消息给 Agent |
| `/run/stream` | POST | SSE 流式发送消息给 Agent，返回 token、思考、工具调用和完成事件 |
| `/sessions` | GET | 列出所有会话 |
| `/sessions` | POST | 创建新会话 |
| `/sessions/{id}` | GET | 获取会话消息历史 |
| `/sessions/{id}` | DELETE | 删除会话 |
| `/sessions/{id}/rename` | PUT | 重命名会话 |
| `/skills` | GET | 列出已加载技能（含格式、来源、触发词、是否声明 MCP） |
| `/skills/reload` | POST | 热加载所有技能 |
| `/subagents` | GET | 列出可用子代理类型 |
| `/subagents/tasks/{id}` | GET | 查询子代理任务状态 |
| `/settings` | GET | 获取当前配置 |
| `/settings` | POST | 保存配置并重启 Agent |
| `/usage` | GET | 今日用量统计 |
| `/usage/session` | GET | 当前会话用量 |
| `/usage/history` | GET | 历史用量（支持 `?days=7`） |
| `/memories` | GET | 列出或搜索长期记忆 |
| `/memories` | POST | 保存长期记忆 |
| `/memories/{key}` | DELETE | 删除长期记忆 |
| `/users` | GET | 列出用户 |
| `/users` | POST | 创建用户 |
| `/users/{user_id}` | DELETE | 删除用户及其数据 |
| `/users/me` | GET | 获取当前登录用户 |
| `/health` | GET | 健康检查 |

---

## 子代理

当前版本提供同步子代理 MVP，主 Agent 可通过 `delegate_task` 工具委派独立任务：

- `coder`：编码实现建议和修改执行
- `reviewer`：代码审查，重点检查 bug、回归风险、边界条件和缺失测试
- `debugger`：系统化排查问题，输出假设、验证步骤和根因判断

第一版 `delegate_task` 会同步等待子代理完成并返回结果。内部已经保留 `task_id`、`status`、`result`、`error`、时间戳等任务状态，可通过 `/subagents/tasks/{id}` 查询。后续要扩展并行执行时，可以在现有 `SubagentManager` 上增加异步 `task_create` / `task_result` API，而不需要推翻工具和状态结构。

当前限制：

- 子代理不是独立进程，仍运行在同一服务进程内。
- 第一版不做真正后台并行，也没有 team mode 成员通信。
- 子代理可用工具由主服务配置，默认会过滤掉 `delegate_task`，避免递归委派。

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

将 Skill 目录放到 `agent_core/samples/`、项目内 `.opencode/skills/`、`.claude/skills/`、`.agents/skills/`，或通过 `AGENT_SKILLS_DIR` 指定目录，然后调用 `/skills/reload` 即可热加载。

### oh-my-openagent Skill 兼容

当前已支持第一阶段兼容：可以直接加载带 YAML frontmatter 的 `SKILL.md`，并自动扫描 oh-my-openagent 常见目录：

- `agent_core/samples/`
- `~/.config/opencode/skills/`
- 当前项目下的 `.opencode/skills/`
- 当前项目下的 `.claude/skills/`
- 当前项目下的 `.agents/skills/`

也可以通过 `AGENT_SKILLS_DIR` 指定额外目录；多个目录用系统路径分隔符连接（macOS/Linux 为 `:`，Windows 为 `;`）。

```markdown
---
name: router-project-helper
description: 智能路由项目脚手架助手
triggers: [智能路由, 路由项目, 脚手架]
mcp:
  servers:
    example: {}
---

当用户需要创建智能路由项目时：
1. 先确认目标平台和路由协议
2. 生成项目目录结构
3. 输出 README 和启动脚本
```

说明：
- `name`、`description`、`trigger` / `triggers` 会被解析为技能元数据。
- 如果没有 `## Instructions`，frontmatter 后的正文会作为技能说明注入系统提示词。
- `mcp` 字段当前只会被识别并在 `/skills` 中标记，暂不启动或执行 MCP Server。

### 当前随项目导入的 Skills

项目内 `.opencode/skills/` 目前保留少量适配当前系统的纯提示词型技能：

- `frontend-ui-ux`：界面与交互体验设计
- `brainstorming`：需求澄清和方案推敲
- `writing-plans`：实现计划拆解
- `executing-plans`：按计划执行
- `test-driven-development`：TDD 红绿重构流程
- `systematic-debugging`：系统化定位根因
- `verification-before-completion`：完成前验证
- `receiving-code-review`：处理代码评审反馈

加上内置示例 `agent_core/samples/daily-report`，默认会加载 9 个 Skills。侧边栏会展示已加载技能，列表区域固定高度并支持滚动；鼠标悬停在技能名称上会显示描述和触发词。

用户询问“你有哪些技能”“已加载哪些 Skills”等问题时，后端会直接从 `SkillRegistry` 返回真实技能清单，避免模型把底层工具能力误报为 Skills。

---

## 配置说明

配置文件路径：`~/.desktop_agent/config.json`

登录凭据文件路径：`~/.desktop_agent/auth.json`

用户数据根目录：`~/.desktop_agent/users/{user_id}/`

会话数据库路径：`~/.desktop_agent/users/{user_id}/sessions/sessions.sqlite3`

用量数据库路径：`~/.desktop_agent/users/{user_id}/usage/usage.sqlite3`

长期记忆文件路径：`~/.desktop_agent/users/{user_id}/memory/*.json`

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

### URL Token 登录

`generate_login_url.py` 会读取 `~/.desktop_agent/auth.json` 中的签名密钥，生成默认 5 分钟有效的登录链接。多用户场景可以用 `--user` 指定用户：

```bash
python generate_login_url.py --host 192.168.1.100 --port 8899 --expires 300 --user test
```

服务端验证通过后会写入正常的 7 天会话 Cookie，并跳转到主页。Token 过期或签名无效时会提示重新登录。公网部署时仍建议优先使用固定密码和 HTTPS，URL Token 只用于可信场景下的临时访问。

### 用量统计说明

用量记录按用户保存在 `~/.desktop_agent/users/{user_id}/usage/usage.sqlite3` 的 `usage_records` 表中。

- `model_calls`：模型调用次数
- `tool_calls`：工具调用次数
- `total_input_tokens` / `total_output_tokens`：只统计模型 Token
- `provider_breakdown`：按模型厂商聚合
- `model_breakdown`：按 `provider:model` 聚合
- `tool_breakdown`：按工具名统计调用次数

工具返回内容不会计入模型 Token。

### 长期记忆说明

长期记忆用于保存跨会话、跨重启仍有价值的信息，例如：

- 用户偏好：默认语言、回答风格
- 项目事实：部署目录、端口、常用服务器
- 长期约定：公网部署必须开启登录保护

当前采用显式记忆策略：只有用户明确要求“记住/以后记得/保存偏好”时，Agent 才会调用 `remember` 工具写入当前登录用户的长期记忆。不要保存 API Key、密码、Cookie、Token 等敏感信息。

页面顶部 **记忆** 按钮可打开记忆管理面板，支持新增、搜索和删除。

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
