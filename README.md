# Desktop Agent

Desktop Agent 是一个本地/私有部署的桌面 AI 智能体。它基于 FastAPI、LangGraph 和 OpenAI 兼容模型接口，提供聊天式任务执行、工具调用、文件制品下载、Skills 技能扩展、多用户隔离、长期记忆和可视化执行过程。

适合用作个人或团队内网的项目助手：读写工作区文件、运行 Python、搜索网页、管理 Git、生成文档、分析图片、委派子代理处理独立任务。

---

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 聊天式 Agent | LangGraph ReAct Agent，支持流式输出、思考/工具步骤展示、长任务进度提示和终止任务 |
| 多行与图片输入 | 输入框支持多行文本、粘贴图片；图片只用于当轮分析，不把 base64 写入历史 |
| 多模态模型切换 | MiMo 系列发图时，如果当前模型不支持图片，会本轮临时切换到 `mimo-v2.5` |
| 文件工具 | 读写/追加/删除/列出/搜索工作区文件，大文件只返回摘要和路径，避免撑爆上下文 |
| 文件制品 | AI 生成工作区文件后自动追加下载链接；Markdown 文件支持弹窗预览 |
| Python 执行 | 运行 Python 代码并返回输出；超大输出只返回摘要、开头和结尾 |
| 网页能力 | `web_search` 搜索网页，`web_fetch` 抓取正文；带重试和兜底请求 |
| Git 工具 | 查看状态、diff、日志、show；按明确指令 add/commit/push/revert |
| 子代理 | `delegate_task` 委派 coder / reviewer / debugger，同步执行，状态结构已为并行预留 |
| Skills | 加载 `SKILL.md`，兼容 YAML frontmatter 和 oh-my-openagent / Superpowers 风格技能 |
| 长期记忆 | 按用户隔离保存长期偏好、项目事实和常用环境信息 |
| 多用户 | 登录保护、管理员用户管理、每个用户独立工作区、会话、用量和记忆 |
| 上下文管理 | 按模型上下文窗口估算长度，达到阈值时压缩历史；大日志/大文件不直接塞全文 |
| 用量统计 | 按用户、会话、Provider、模型和工具统计调用与 token |

---

## 快速开始

### 环境要求

- Python 3.10+，推荐 3.12/3.13
- 一个 OpenAI 兼容模型 API Key
- 可选：Git、curl、Windows 打包环境

### 启动

```bash
git clone https://gitee.com/mingwuda/desktop-agent.git
cd desktop-agent
pip install -r requirements.txt
./start.sh
```

启动后打开：

```text
http://127.0.0.1:8899/
```

也可以直接启动后端：

```bash
cd agent_core
python main.py
```

Windows 可执行：

```cmd
start.cmd
```

首次启动会创建配置、认证和用户数据目录。默认如果没有配置用户，会创建 `admin / admin123`，正式部署请立即修改。

---

## 登录与多用户

服务默认启用登录保护。认证文件位于：

```text
~/.desktop_agent/auth.json
```

可通过环境变量初始化登录配置：

```bash
export DESKTOP_AGENT_AUTH_USER=admin
export DESKTOP_AGENT_AUTH_PASSWORD='your-strong-password'
export DESKTOP_AGENT_AUTH_SECRET='replace-with-a-random-secret'
```

支持短期免密登录链接：

```bash
python generate_login_url.py --host 127.0.0.1 --port 8899 --expires 300 --user admin
```

可选参数：

- `--qr`：在终端显示二维码
- `--copy`：复制到剪贴板

多用户数据按用户隔离：

```text
~/.desktop_agent/users/{user_id}/sessions/sessions.sqlite3
~/.desktop_agent/users/{user_id}/usage/usage.sqlite3
~/.desktop_agent/users/{user_id}/memory/
~/agent_workspace/{user_id}/
```

只有 `admin` 用户能看到设置和用户管理相关入口。

---

## 模型配置

在页面右上角点击“设置”，可以配置：

| 字段 | 说明 |
| --- | --- |
| 模型厂商 | OpenAI、DeepSeek、通义千问或自定义 Provider |
| API Key | 模型服务密钥 |
| 模型名称 | 例如 `gpt-4o`、`deepseek-chat`、`qwen-plus`、`mimo-v2.5-pro` |
| API 地址 | OpenAI 兼容地址，留空则使用 OpenAI 默认地址 |
| 最大推理步数 | LangGraph 单次任务最大循环步数，默认 `60` |
| 请求重试次数 | 模型连接错误重试次数，默认 `3` |
| 请求超时 | 模型读取超时，默认 `30` 秒 |
| 上下文窗口 | 当前模型最大上下文长度；留空时按模型名内置估算 |

配置保存到：

```text
~/.desktop_agent/config.json
```

常用环境变量：

| 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `LLM_PROVIDER` | 当前 Provider | `openai` |
| `LLM_API_KEY` / `OPENAI_API_KEY` | API Key | 空 |
| `LLM_MODEL` | 模型名称 | `gpt-4o` |
| `LLM_BASE_URL` / `OPENAI_BASE_URL` | API 地址 | 空 |
| `AGENT_WORKSPACE` | 工作区根目录 | `~/agent_workspace` |
| `AGENT_SKILLS_DIR` | 额外 Skills 目录 | 内置 samples |
| `AGENT_HOST` | 监听地址 | `127.0.0.1` |
| `AGENT_PORT` | 监听端口 | `8899` |
| `AGENT_RECURSION_LIMIT` | 最大推理步数 | `60` |
| `AGENT_API_MAX_RETRIES` | 模型连接错误重试次数 | `3` |
| `AGENT_API_TIMEOUT_SECONDS` | 模型请求超时秒数 | `30` |
| `AGENT_CONTEXT_WINDOW_TOKENS` | 手动指定模型上下文窗口 | 自动识别 |
| `AGENT_API_HOST_IPS` | 自定义模型网关 DNS 兜底 IP 列表 | 空 |
| `DESKTOP_AGENT_AUTH_COOKIE_SECURE` | Cookie 是否仅 HTTPS 发送 | `0` |

`AGENT_API_HOST_IPS` 只用于 DNS/网络排障，服务不会内置任何厂商 IP。多个 IP 用英文逗号分隔。

### 图片输入与视觉模型

前端支持直接粘贴图片到输入区。后端会把图片转换为 OpenAI 兼容的 `image_url` 消息格式。

图片不会写入 SQLite 历史，也会在本轮完成后从 LangGraph checkpoint 中清理，避免后续纯文本消息重复携带图片。

MiMo 系列模型有特殊处理：当本轮包含图片，且当前模型名包含 `mimo` 但不是 `mimo-v2.5` / `mimo-v2-omni` 时，系统会临时使用 `mimo-v2.5` 执行本轮请求，并在前端显示提示。默认模型设置不会被修改。

---

## 使用方式

输入框支持：

- `Enter` 换行
- `Cmd/Ctrl + Enter` 发送
- 直接粘贴图片
- 执行中点击“停止”终止当前任务

示例问题：

```text
帮我列出工作区文件
写一段 Python 代码计算斐波那契数列并运行
搜索今天的 AI 新闻并总结
打开这个链接并总结正文：https://example.com/article
看一下这个仓库当前有哪些改动
把当前改动提交一下，提交信息是：完善 Skills 支持
帮我 push 当前分支
回退上一个提交
记住：我希望回复默认使用中文
```

### Git 工具边界

Git 工具包括：

```text
git_status
git_diff
git_log
git_show
git_add
git_commit
git_commit_all
git_push
git_revert
git_command
```

安全策略：

- 只有用户明确要求提交时才使用 `git_add` / `git_commit` / `git_commit_all`
- 只有用户明确要求推送时才使用 `git_push`
- 只有用户明确要求回退版本时才使用 `git_revert`
- `git_push` 只允许普通 push、指定 remote/branch、首次设置 upstream
- `git_revert` 只允许单个 revision，支持 `--no-commit`
- 不开放 `pull`、`reset`、`restore`、force push、range revert、merge revert 等高风险操作

---

## 文件制品

如果 Agent 使用 `write_file` 或 `append_to_file` 在工作区生成文件，最终回复会自动追加“可下载文件”区域。

Markdown 文件会同时提供：

- `预览`：在页面弹窗中渲染 Markdown
- `下载`：直接下载原文件

普通文件只提供下载链接。

---

## 长上下文处理

系统会估算 LangGraph checkpoint 中的消息长度，并根据模型最大上下文窗口的 80% 作为压缩阈值。

内置识别示例：

| 模型 | 上下文窗口 |
| --- | --- |
| `gpt-4o` / `gpt-4o-mini` | 128K |
| `gpt-4.1` / `gpt-4.1-mini` | 1M |
| `qwen-long` | 1M |
| `mimo-v2.5-pro` / `mimo-*` | 1M |
| `deepseek-chat` / `deepseek-reasoner` | 64K |

达到阈值后会压缩旧消息，保留最近上下文。完整历史仍在 SQLite 中，可通过会话记录查看。

大文件、大日志和 Python 超大输出不会完整塞入模型上下文，只返回摘要、路径、开头和结尾。

---

## Skills

Desktop Agent 会加载以下目录中的 `SKILL.md`：

```text
agent_core/samples/
AGENT_SKILLS_DIR 指定目录
项目内 .opencode/skills/
项目内 .claude/skills/
项目内 .agents/skills/
```

支持两种格式：

1. 简单 Markdown 章节：`Description`、`Trigger`、`Instructions`
2. YAML frontmatter：兼容 oh-my-openagent / Superpowers 风格

示例：

```markdown
---
name: systematic-debugging
description: 系统化定位根因
triggers: [debug, 排查, 根因]
---

当用户需要排查问题时：
1. 先复现现象
2. 列出假设
3. 逐步验证
4. 给出根因和修复建议
```

侧边栏会显示已加载技能，区域固定高度并支持滚动；鼠标悬停会展示描述和触发词。用户问“你有哪些技能”时，后端会直接返回真实 SkillRegistry 内容，避免模型误报。

当前项目内置/随项目保留的技能包括：

- `daily-report`
- `frontend-ui-ux`
- `brainstorming`
- `writing-plans`
- `executing-plans`
- `test-driven-development`
- `systematic-debugging`
- `verification-before-completion`
- `receiving-code-review`

---

## 子代理

`delegate_task` 可以把独立任务委派给子代理：

| 子代理 | 用途 |
| --- | --- |
| `coder` | 编码实现、局部修改 |
| `reviewer` | 代码审查、风险和缺失测试检查 |
| `debugger` | 系统化排障、根因定位 |

当前版本是同步 MVP：主 Agent 会等待子代理完成再继续。内部保留了 `task_id`、`status`、`result`、`error`、时间戳等状态结构，后续可以在此基础上扩展并行执行。

为了避免递归委派，子代理默认不能再调用 `delegate_task`。

---

## 长期记忆

长期记忆按用户隔离，适合保存：

- 用户偏好：默认语言、回答风格
- 项目事实：部署目录、端口、常用服务器
- 长期约定：公网部署必须开启登录保护

工具：

```text
remember
recall_memory
forget_memory
list_memories
```

当前采用显式记忆策略：只有用户明确要求“记住/以后记得/保存偏好”时才写入。不要保存 API Key、密码、Cookie、Token 等敏感信息。

页面顶部“记忆”按钮可打开管理面板，支持新增、搜索和删除。

---

## API

启动后访问：

```text
http://127.0.0.1:8899/docs
```

主要接口：

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET | 桌面 UI |
| `/login` | GET | 登录页 |
| `/auth/login` | POST | 登录 |
| `/auth/logout` | POST | 退出 |
| `/auth/token-login` | GET | 短期 Token 登录 |
| `/run` | POST | 非流式执行 |
| `/run/stream` | POST | SSE 流式执行 |
| `/sessions` | GET/POST | 列出或创建会话 |
| `/sessions/{id}` | GET/DELETE | 获取或删除会话 |
| `/sessions/{id}/rename` | PUT | 重命名会话 |
| `/skills` | GET | 列出 Skills |
| `/skills/reload` | POST | 热加载 Skills |
| `/subagents` | GET | 列出子代理类型 |
| `/subagents/tasks/{id}` | GET | 查询子代理任务 |
| `/memories` | GET/POST | 查询或保存长期记忆 |
| `/memories/{key}` | DELETE | 删除长期记忆 |
| `/artifacts/download` | GET | 下载工作区文件制品 |
| `/artifacts/preview` | GET | 预览 Markdown 制品 |
| `/settings` | GET/POST | 读取或保存配置 |
| `/usage` | GET | 今日用量 |
| `/usage/session` | GET | 会话用量 |
| `/usage/history` | GET | 历史用量 |
| `/users` | GET/POST | 管理用户 |
| `/users/{user_id}` | DELETE | 删除用户 |
| `/users/me` | GET | 当前登录用户 |
| `/health` | GET | 健康检查 |

除登录、退出、Token 登录和健康检查外，其它 API 都需要登录。

---

## 远程部署

示例部署到 `/opt/desktop-agent`，监听 `8080`：

```bash
git clone https://gitee.com/mingwuda/desktop-agent.git /opt/desktop-agent
cd /opt/desktop-agent
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt
```

systemd 示例：

```ini
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

启动：

```bash
systemctl daemon-reload
systemctl enable desktop-agent
systemctl restart desktop-agent
systemctl status desktop-agent
```

查看日志：

```bash
journalctl -u desktop-agent -f
```

公网部署建议放在 HTTPS 反向代理后，并设置强密码和固定 `DESKTOP_AGENT_AUTH_SECRET`。

---

## Windows 打包

生成可分发包：

```cmd
packaging\windows\build.cmd
```

或：

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1
```

输出：

```text
dist\windows\DesktopAgent-Windows\
dist\windows\DesktopAgent-Windows.zip
```

用户解压后双击 `Start Desktop Agent.bat` 即可启动。配置、会话和工作区默认保存在用户目录。

依赖排查：

```cmd
packaging\windows\verify-venv.cmd
packaging\windows\verify-venv.cmd run
```

内网 PyPI：

```cmd
set DESKTOP_AGENT_PIP_INDEX_URL=http://your-internal-pypi/simple/
set DESKTOP_AGENT_PIP_TRUSTED_HOST=your-internal-pypi-host
```

---

## 项目结构

```text
desktop-agent/
├── agent_core/
│   ├── main.py                 # FastAPI 入口、API、认证、用户与制品路由
│   ├── agent.py                # DesktopAgent、LangGraph、流式事件、上下文处理
│   ├── config.py               # Provider、模型、环境变量和配置持久化
│   ├── context_manager.py      # 上下文估算、阈值和压缩
│   ├── subagents.py            # coder/reviewer/debugger 子代理
│   ├── session_store.py        # SQLite 会话存储
│   ├── user_manager.py         # 多用户数据目录
│   ├── memory/
│   │   └── local_memory.py     # 长期记忆
│   ├── monitoring/
│   │   └── usage_tracker.py    # 用量统计
│   ├── skills/
│   │   ├── loader.py           # SKILL.md 解析
│   │   └── registry.py         # SkillRegistry
│   └── tools/
│       ├── file_tools.py       # 文件工具
│       ├── code_tools.py       # Python 执行
│       ├── git_tools.py        # Git 工具
│       ├── web_tools.py        # 搜索和网页抓取
│       ├── memory_tools.py     # 记忆工具
│       └── system_tools.py     # 系统信息和 Skills 列表
├── desktop/
│   └── index.html              # 单页前端
├── .opencode/skills/           # 项目内 Skills
├── packaging/windows/          # Windows 打包脚本
├── start.sh
├── start.cmd
├── generate_login_url.py
└── requirements.txt
```

---

## 技术栈

| 层面 | 技术 |
| --- | --- |
| Agent | LangGraph ReAct Agent |
| LLM 接口 | LangChain OpenAI，兼容 OpenAI 风格接口 |
| 后端 | FastAPI + Uvicorn |
| 前端 | 原生 HTML/CSS/JS + marked.js |
| 状态 | LangGraph MemorySaver + SQLite |
| 认证 | HttpOnly Cookie + HMAC 签名 |
| 运行时 | Python 3.10+ |

---

## 安全边界

- 工具默认限制在当前用户工作区内。
- 文件下载和预览只能访问当前用户工作区内的文件。
- Git 高风险命令默认不开放。
- 图片不会持久化保存到历史数据库。
- 长期记忆需要用户明确要求才写入。
- 公网部署必须配置强密码、固定密钥和 HTTPS。
