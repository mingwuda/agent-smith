# AgentSmith

[中文](README.md) | [English](README.en.md)


AgentSmith 是一个本地/私有部署的桌面 AI 智能体。它基于 FastAPI、LangGraph 和 OpenAI 兼容模型接口，提供聊天式任务执行、工具调用、文件制品下载、Skills 技能扩展、多用户隔离、长期记忆和可视化执行过程。

适合用作个人或团队内网的项目助手：读写工作区文件、运行 Python、搜索网页、管理 Git、生成文档、分析图片、委派子代理处理独立任务。

---

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 聊天式 Agent | LangGraph ReAct Agent，支持流式输出、思考/工具步骤展示、长任务进度提示和终止任务 |
| 多行与图片输入 | 输入框支持多行文本、粘贴图片；图片只用于当轮分析，不把 base64 写入历史 |
| 文件与 ZIP 上传 | 支持上传图片（分析视觉内容）和 ZIP 压缩包（自动解压到工作区，生成文件清单供 AI 分析项目结构） |
| 多模态模型切换 | MiMo 系列发图时，如果当前模型不支持图片，会本轮临时切换到 `mimo-v2.5` |
| 文件工具 | 读写/追加/删除/列出/搜索工作区文件，大文件只返回摘要和路径，避免撑爆上下文 |
| 文件制品 | AI 生成工作区文件后自动追加下载链接；Markdown 文件支持弹窗预览 |
| 文件 Diff 可视化 | Agent 修改文件后，前端实时展示绿/红高亮的行级变更对比，支持折叠展开 |
| Todo 清单 | Agent 自动将复杂任务拆解为 Todo 清单，逐项跟踪完成进度，支持批量完成和状态同步 |
| Python 执行 | 运行 Python 代码并返回输出；超大输出只返回摘要、开头和结尾；**支持实时流式输出**，避免用户空等 |
| 网页能力 | `web_search` 搜索网页（Bing → 搜狗 → DuckDuckGo 逐级 fallback），`web_fetch` 抓取正文 |
| Git 工具 | 查看状态、diff、日志、show、worktree；按明确指令 add/commit/push/revert/merge/checkout |
| Shell 命令 | `run_shell` 执行 shell 命令（bash/zsh/sh/powershell/cmd），自动跨平台适配，内置安全拦截 |
| 浏览器自动化 | 内置 Playwright 浏览器，支持导航、点击、填表、截图、JS 执行等操作；截图通过 token URL 访问，避免路径泄露 |
| 验证码识别 | 基于多模态大模型的验证码自动识别，支持扭曲文字/滑块/图标点选/汉字点选等类型；验证码元素自动定位、坐标自适应缩放、低置信度自动刷新重试 |
| 子代理 | `delegate_task` 串行 + `delegate_tasks_parallel` 并行委派 coder/reviewer/debugger/searcher；**支持实时日志流**展示执行过程 |
| Skills | 加载 `SKILL.md`，兼容 YAML frontmatter 和 oh-my-openagent / Superpowers 风格技能；内置 `database-interaction` 技能支持自然语言数据库交互 |
| 长期记忆 | 按用户隔离保存长期偏好、项目事实和常用环境信息 |
| 数据库交互 | 内置 `dbcli` 核心库 + CLI 工具 + Agent 技能，支持 SQLite / PostgreSQL / MySQL 自然语言查询，列级/行级权限控制 |
| 多用户 | 登录保护、管理员用户管理、每个用户独立工作区、会话、用量和记忆；支持 `AGENT_USERS` 环境变量批量配置 |
| 上下文管理 | 按模型上下文窗口估算长度，达到阈值时压缩历史；大日志/大文件不直接塞全文 |
| 用量统计 | 按用户、会话、Provider、模型和工具统计调用与 token |
| 工具卡片 | 步骤卡片带绿色左边框、工具名 + 耗时 + 状态圆点、参数/结果分栏展示，支持折叠

---

## 快速开始

### 环境要求

- Python 3.9+，推荐 3.10+（3.9 通过 `from __future__ import annotations` 兼容）
- 一个 OpenAI 兼容模型 API Key
- 可选：Git、curl、Windows 打包环境

### 启动

```bash
git clone https://gitee.com/mingwuda/desktop-agent.git
cd desktop-agent
pip install -r requirements.txt
./start.sh
```

或使用 Docker 一键部署（见下方 [远程部署 → Docker](#docker推荐)）。

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
# 多用户（推荐，Docker / systemd 环境）
export AGENT_USERS='admin:your-password;guest:guest123'

# 或单用户（DEPRECATED）
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
| `AGENT_PORT` / `DESKTOP_AGENT_PORT` | 监听端口 | `8899` |
| `AGENT_RECURSION_LIMIT` | 最大推理步数 | `60` |
| `AGENT_API_MAX_RETRIES` | 模型连接错误重试次数 | `3` |
| `AGENT_API_TIMEOUT_SECONDS` | 模型请求超时秒数 | `120` |
| `AGENT_CONTEXT_WINDOW_TOKENS` | 手动指定模型上下文窗口 | 自动识别 |
| `AGENT_API_HOST_IPS` | 自定义模型网关 DNS 兜底 IP 列表 | 空 |
| `AGENT_USERS` | 多用户列表 `user1:pass1;user2:pass2` | 空 |
| `AGENT_SHARED_TOKEN` | 共享 API Token | 空 |
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
帮我规划一个博客系统，列出 Todo 清单并逐步完成
帮我分析这个项目的代码结构，生成一个改进建议报告
```

### Git 工具边界

Git 工具包括：

```text
git_status       git_diff       git_log        git_show
git_add          git_commit     git_commit_all git_push
git_revert       git_command
```

`git_command` 安全白名单：

| 子命令 | 允许操作 |
|--------|---------|
| `status` / `diff` / `log` / `show` | 查看 |
| `add` | 暂存文件 |
| `commit -m` | 提交 |
| `push` / `push -u origin branch` | 推送 |
| `revert <revision>` | 回退提交 |
| `branch` / `branch -a` / `branch -d/-D <name>` | 查看/删除分支 |
| `checkout <branch>` / `checkout -b <branch>` | 切换/创建分支 |
| `merge <branch>` | 合并分支 |
| `worktree list / add / remove / prune` | 工作树管理 |
| `remote -v` | 查看远程仓库 |

安全策略：

- 只有用户明确要求提交时才使用 `git_add` / `git_commit` / `git_commit_all`
- 只有用户明确要求推送时才使用 `git_push`
- 只有用户明确要求回退版本时才使用 `git_revert`
- `git_push` 只允许普通 push、指定 remote/branch、首次设置 upstream
- `git_revert` 只允许单个 revision，支持 `--no-commit`
- 不开放 `pull`、`reset`、`restore`、force push、range revert、merge revert 等高风险操作

### Shell 工具边界

`run_shell` 工具支持跨平台 shell 命令执行：

| 平台 | 使用的 Shell |
|------|-------------|
| Linux/macOS | bash → zsh → sh（自动检测） |
| Windows | powershell → cmd（自动检测） |

安全策略：

- 禁止提权操作（`sudo`、`su`）
- 禁止格式化磁盘（`mkfs`、`mkswap`、`dd if=`）
- 禁止 fork bomb 和管道关机
- 默认 120 秒超时（最大 600 秒）
- 输出超过 20000 字符时自动截断（保留头 8000 + 尾 8000）
- 自动对比执行前后工作区文件变更并汇总

### 浏览器自动化

AgentSmith 内置 Playwright 浏览器，支持完整的页面交互流程：

| 工具 | 用途 |
|------|------|
| `browser_navigate` | 导航到指定 URL |
| `browser_click` | 点击 CSS 选择器匹配的元素 |
| `browser_fill` | 在输入框中填入文本 |
| `browser_select` | 选择下拉框选项 |
| `browser_get_text` | 获取元素文本内容 |
| `browser_screenshot` | 截图（全页或视口） |
| `browser_evaluate` | 执行 JavaScript |
| `browser_wait` | 等待指定时长 |
| `browser_scroll_to` | 滚动页面到元素或坐标 |
| `browser_wait_for_element` | 等待元素出现/可见 |
| `browser_drag` | 拖拽元素到目标位置 |
| `browser_slide` | 滑块操作（含人类化轨迹模拟） |

### 验证码识别

基于多模态大模型（如 step-3.7-flash、gpt-4o）的验证码自动识别与交互：

| 验证码类型 | 识别方式 |
|------------|----------|
| 扭曲字母/数字 | 直接返回字符，调用 `browser_fill` 填入 |
| 文字点选（汉字） | 识别汉字位置和顺序点击 |
| 图标点选 | 识别图标名称和位置，按顺序点击 |
| 滑块验证码 | 标记类型，引导 `browser_slide` 拖动 |

**工具链：**

1. `browser_captcha_recognize(source="page")` — 全页截图检测验证码
2. `browser_captcha_recognize(source="selector:#captcha")` — 验证码元素特写截图，精确坐标
3. `browser_captcha_scan_grid(rows=9, cols=16)` — 叠加 SVG 网格辅助定位
4. `browser_click_captcha(clicks=...)` — 视口坐标点击
5. `browser_captcha_click_sequence(selector, clicks, ...)` — 元素内坐标点击
6. `browser_captcha_refresh(selector)` — 低置信度时自动刷新验证码重试

**低置信度处理：** 识别置信度低于 0.5 时自动建议刷新验证码重试。

**日志上下文：** 每条日志携带 `[s:sessionId] [m:messageId]` 前缀，支持按会话和消息维度检索。

### 文件工具边界

`write_file` / `append_to_file` / `edit_file` 等写操作默认只能修改工作区内的文件。
如需编辑工作区外的文件，前端工具卡片会显示 **「授权写入」** 按钮：

1. 点击后授权该文件路径（同时自动授权其父目录）
2. 授权仅当前进程有效，重启后需要重新授权
3. 也可通过设置会话工作目录来扩大允许范围

### 会话工作目录

每个会话可以设置独立的工作目录，切换会话时自动恢复。工作目录内的文件操作不受"路径超出工作区"限制。

在顶部栏点击 📁 路径显示区域，输入绝对路径或相对于默认工作区的路径即可设置。设置后的目录会自动创建（如不存在），并立即生效。

支持 API：

- `PUT /sessions/{session_id}/workspace` — 设置工作目录
- `GET /sessions/{session_id}/workspace` — 获取工作目录

## 文件制品

如果 Agent 使用 `write_file` 或 `append_to_file` 在工作区生成文件，最终回复会自动追加“可下载文件”区域。

Markdown 文件会同时提供：

- `预览`：在页面弹窗中渲染 Markdown
- `下载`：直接下载原文件

普通文件只提供下载链接。

---

## Python 实时输出流

当 Agent 执行 `run_python` 时，输出会**实时流式推送**到前端的终端风格黑底代码块中，无需等待脚本执行完毕即可看到中间输出。

适用于长时间运行的脚本（如数据爬取、模型训练、批量处理）。

---

## ZIP 文件上传与分析

支持上传 `.zip` 压缩包（最大 50MB），后端自动解压到工作区并生成文件清单：

```
[DIR]  src/
[FILE] src/main.py  (2.3KB)
[FILE] src/utils.py (1.1KB)
...
```

LLM 会直接看到项目结构，可以据此分析代码、给出建议或执行后续操作。解压后的文件保留在工作区 `~/.agent_zip/{name}_{hash}/` 目录下，Agent 可直接读写。

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

AgentSmith 会加载以下目录中的 `SKILL.md`：

```text
agent_core/samples/
AGENT_SKILLS_DIR 指定目录
项目内 .opencode/skills/
项目内 skills/
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

当前项目内置/随项目保留的技能包括 14 个：

| 技能 | 来源 | 用途 |
|------|------|------|
| `daily-report` | 内置示例 | 日报生成 |
| `brainstorming` | oh-my-openagent | 需求澄清、方案设计 |
| `writing-plans` | oh-my-openagent | 生成实施计划 |
| `executing-plans` | oh-my-openagent | 批量执行计划 |
| `test-driven-development` | oh-my-openagent | TDD 红绿重构 |
| `systematic-debugging` | oh-my-openagent | 四阶段系统化调试 |
| `verification-before-completion` | oh-my-openagent | 完成前验证 |
| `receiving-code-review` | oh-my-openagent | 接收代码审查反馈 |
| `frontend-ui-ux` | oh-my-openagent | 前端 UI/UX 设计 |
| `subagent-driven-development` | Superpowers | 子代理派发 + 两阶段审查（P0） |
| `requesting-code-review` | Superpowers | 主动代码审查，严重等级评估（P0） |
| `dispatching-parallel-agents` | Superpowers | 并行派发独立子代理（P1） |
| `finishing-a-development-branch` | Superpowers | 开发分支收尾清理（P2） |
| `using-git-worktrees` | Superpowers | Git Worktree 隔离开发环境（P1） |
| `database-interaction` | 内置 | 自然语言数据库交互 |

---

## 微信集成

AgentSmith 通过**腾讯官方 iLink Bot API** 接入微信个人号，让你可以直接在微信里与 Agent 对话。

### 特性

- **官方合法**：基于腾讯 iLink 协议，有法律条款背书，无封号风险
- **无需公网 IP**：客户端主动长轮询微信服务器，不需要 ngrok / frp / 公网域名
- **扫码一次，永久使用**：Token 持久化到本地文件，服务重启后自动恢复轮询
- **多用户隔离**：每个平台用户可绑定独立的微信 Bot，token、会话、轮询完全隔离
- **信息零存储**：腾讯仅做消息管道中转，不存储你的消息内容和 AI 输出
- **图文消息**：支持接收用户发送的图片+文字提问，也支持 Agent 生成图片后主动向用户发送图文消息（CDN 上传 + AES 加密）
- **会话同步**：微信中的对话自动保存到 Web 端会话列表，带 💬 图标标识；同名 ID 的 Web 和微信会话各自独立展示

### 使用步骤

#### 1. 安装依赖

```bash
pip install qrcode[pil]
```

#### 2. 重启服务

确保后端已加载最新代码（`agent_core/wechat_bot.py`）。

#### 3. 扫码登录

浏览器访问：

```
http://localhost:8899/wechat/qrcode
```

页面会显示微信登录二维码，用手机微信扫码并确认。扫码成功后 Bot 自动开始轮询消息。

如果是多用户环境，每个用户访问 `/wechat/qrcode` 都会生成**属于该用户的二维码**，token 按用户隔离存储。

#### 4. 开始使用

在微信里给 Bot 发送消息，Agent 会自动回复。回复内容会同步显示在 Web 端的会话列表中（带 💬 图标标记）。

支持在微信内通过指令管理会话：

| 指令 | 说明 |
|------|------|
| `/new` | 创建新会话 |
| `/list` | 列出所有会话，显示 sessionId 和最后一条用户消息，当前会话用 `→` 标记 |
| `/switch <sessionId>` | 切换到指定会话 |

### 管理端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/wechat/qrcode` | GET | 微信扫码登录页面（返回 HTML，按当前登录用户生成二维码） |
| `/wechat/qrcode-status?qrcode=xxx` | GET | 轮询扫码状态 |
| `/wechat/status` | GET | 查看当前用户的登录/运行状态 |
| `/wechat/start` | POST | 手动启动当前用户的消息轮询 |
| `/wechat/stop` | POST | 停止当前用户的消息轮询 |
| `/wechat/sessions` | GET | 列出当前用户的微信 Bot 会话列表 |
| `/wechat/sessions/{id}` | GET | 获取当前用户的微信会话消息详情 |

每个用户的 token 存储在独立目录 `~/.desktop_agent/wechat_{user_id}/` 下，互不干扰。服务重启后，所有已登录用户的 Bot 自动恢复轮询。

### 注意事项

- 首次使用必须先扫码登录（一次即可，Token 持久化）
- Agent 处理消息需要一定时间（通常 30-60 秒），处理期间微信会显示"对方正在输入..."
- 目前只支持 1 对 1 私聊，不支持群消息
- 腾讯保留控制权，可能会限速或调整策略

---

## 数据库交互

AgentSmith 内置 `dbcli` 数据库交互系统，让 Agent 可以直接用自然语言与数据库对话。

### 架构

```
用户 ═▶ Agent ═▶ database_tool（Agent 工具）
                         │
                    dbcli 核心库
                  ┌─ auth.py   —— 列级/行级权限控制
                  ├─ connection.py —— SQLAlchemy 连接池
                  ├─ query.py  —— SQL 执行与结果格式化
                  └─ schema.py —— 表结构自省
                         │
              ┌──────────┼──────────┐
           SQLite    PostgreSQL    MySQL
```

### 使用方法

在设置弹窗的「数据库」面板中添加/测试/保存连接，然后 Agent 就能直接查询：

```
用户：帮我查一下 orders 表里上周的订单
Agent：先调用 db_schema 查看 orders 表结构，再生成 SQL 查询并返回结果
```

### 命令行

```bash
cd agent_core
python -m dbcli.cli query "SELECT count(*) FROM users" --conn prod_db
python -m dbcli.cli schema --conn prod_db --table orders
python -m dbcli.cli connect list
python -m dbcli.cli connect test my_db
```

### 权限控制

数据库面板默认开启只读模式，更细粒度的权限在 `~/.desktop_agent/dbcli/permissions.yaml` 中配置：

```yaml
roles:
  analyst:
    databases:
      prod_db:
        - table: orders
          columns_allow: [id, amount, status, created_at]  # 列级白名单
          row_filter: "dept_id = {{user.dept_id}}"          # 行级过滤
          allow_write: false
          max_rows: 200
```

### 配置文件

- 数据库连接：`~/.desktop_agent/dbcli/connections.yaml`
- 权限规则：`~/.desktop_agent/dbcli/permissions.yaml`

---

## 子代理

`delegate_task` 可以把独立任务委派给子代理同步执行，`delegate_tasks_parallel` 可以并行派发多个独立任务。

| 子代理 | 用途 |
| --- | --- |
| `coder` | 编码实现、局部修改 |
| `reviewer` | 代码审查、风险和缺失测试检查 |
| `debugger` | 系统化排障、根因定位 |
| `searcher` | 专精互联网搜索，调用 web_search + web_fetch 整理结果 |

所有子代理都支持**实时日志流**，点击胶囊可查看执行过程（工具调用、AI 思考、结果）。

### 串行执行

```python
delegate_task(task="...", agent_type="coder", context="...")
```
主 Agent 等待子代理完成后继续。适用于有依赖关系的任务。

### 并行执行

```python
delegate_tasks_parallel('''[
  {"task": "任务1", "agent_type": "coder", "context": "..."},
  {"task": "任务2", "agent_type": "coder", "context": "..."}
]''')
```
基于 `ThreadPoolExecutor` 真正并行，同一时间最多 4 个子代理。适用于无文件/数据依赖的任务。

### 安全策略

- 子代理默认不能再调用 `delegate_task` 和 `delegate_tasks_parallel`，避免递归委派
- 每个子代理的 prompt 必须完全自包含
- 并行任务的同一文件同一时间只能被一个子代理修改

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
| `/sessions/{id}/workspace` | GET/PUT | 获取或设置会话工作目录 |
| `/permissions/grant-path` | POST | 授权工作区外路径写入 |
| `/permissions/granted-paths` | GET | 查看已授权的路径列表 |
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
| `/users/me` | GET | 当前登录用户 |
| `/db/connections` | GET/POST | 列出或添加数据库连接 |
| `/db/connections/{name}` | DELETE | 删除数据库连接 |
| `/db/connections/{name}/test` | POST | 测试已保存连接 |
| `/db/test-connection` | POST | 测试未保存连接（表单预测试） |
| `/db/permissions` | GET/PUT | 读写权限配置 |
| `/db/query` | POST | 执行 SQL 查询（含权限检查） |
| `/db/schema/{connection_name}` | GET | 获取数据库表结构 |
| `/wechat/qrcode` | GET | 微信扫码登录页 |
| `/wechat/status` | GET | 微信 Bot 状态 |
| `/wechat/start` | POST | 启动微信轮询 |
| `/wechat/stop` | POST | 停止微信轮询 |
| `/wechat/sessions` | GET | 微信会话列表 |
| `/wechat/sessions/{id}` | GET | 微信会话消息 |
| `/health` | GET | 健康检查 |

除登录、退出、Token 登录和健康检查外，其它 API 都需要登录。

---

## 远程部署

### Docker（推荐）

```bash
git clone https://gitee.com/mingwuda/desktop-agent.git
cd desktop-agent

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 API Key 和用户密码
vim .env
```

`.env` 示例：

```bash
# 模型配置
LLM_API_KEY=sk-your-api-key-here
LLM_MODEL=gpt-4o
LLM_BASE_URL=
LLM_PROVIDER=openai

# 用户管理（多个用户用 ; 分隔，格式 user:password;user2:password2）
AGENT_USERS=admin:your-strong-password;guest:guest123

# 可选
# AGENT_CONTEXT_WINDOW=128000
# AGENT_RECURSION_LIMIT=60
```

启动：

```bash
docker compose up -d --build
```

访问 `http://<服务器IP>:8080`，首次启动自动创建默认用户。配置、会话数据和工作区文件保存在 Docker volumes 中，重启不丢失。

```bash
# 查看日志
docker compose logs -f agent

# 停止
docker compose down

# 数据备份
docker run --rm -v desktop-agent_agent_data:/data -v $(pwd):/backup alpine cp -r /data /backup/agent_data_backup
```

### systemd 部署

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
Description=AgentSmith
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
    │   ├── main.py                 # FastAPI 入口、middleware、Agent 生命周期
    │   ├── agent.py                # DesktopAgent、LangGraph、流式事件、上下文处理
    │   ├── config.py               # Provider、模型、环境变量和配置持久化
    │   ├── context_manager.py      # 上下文估算、阈值和压缩
    │   ├── subagents.py            # coder/reviewer/debugger 子代理 + 并行支持
    │   ├── session_store.py        # SQLite 会话存储（按用户隔离）
    │   ├── user_manager.py         # 多用户数据目录管理
│   ├── api/                    # API 路由模块
│   │   ├── deps.py             # 认证依赖（get_current_user、require_admin）
│   │   ├── auth.py             # 登录/登出/修改密码路由
│   │   └── routes/
│   │       ├── agent.py        # /run、/run/stream 路由
│   │       ├── sessions.py     # 会话 CRUD（Web + 微信独立命名空间）
│   │       ├── skills.py       # Skills 列表与热加载
│   │       ├── artifacts.py    # 文件制品下载与预览
│   │       ├── db.py           # 数据库连接管理
│   │       ├── system.py       # 设置与用户管理
│   │       ├── wechat.py       # 微信 Bot 管理端点
│   │       └── monitoring.py   # 用量统计与健康检查
│   ├── services/
│   │   ├── workspace.py        # 工作区与 ZIP 解压工具
│   │   └── agent_service.py    # Agent 调用封装
│   ├── network_resolver.py     # DNS 兜底解析
│   ├── dbcli/                   # 数据库交互核心库
│   │   ├── auth.py              # 列级/行级权限引擎
│   │   ├── connection.py        # SQLAlchemy 连接池
│   │   ├── query.py             # SQL 执行与结果格式化
│   │   ├── schema.py            # 表结构自省
│   │   ├── config.py            # 连接与权限配置管理
│   │   ├── cli.py               # Click 命令行工具
│   │   └── permissions.yaml     # 权限规则模板
│   ├── memory/
│   │   └── local_memory.py     # 长期记忆
│   ├── monitoring/
│   │   └── usage_tracker.py    # 用量统计
│   ├── skills/
│   │   ├── loader.py           # SKILL.md 解析（支持 YAML frontmatter）
│   │   └── registry.py         # SkillRegistry 技能注册表
│   ├── samples/                # 示例技能
│   └── tools/
│       ├── file_tools.py       # 文件工具（含工作区外授权机制）
│       ├── code_tools.py       # Python 执行
│       ├── shell_tools.py      # Shell 命令执行（跨平台，安全拦截）
│       ├── git_tools.py        # Git 工具（含白名单安全验证）
│       ├── web_tools.py        # 搜索和网页抓取
│       ├── browser_tools.py    # 浏览器自动化 + 验证码识别（Playwright，截图 token URL）
│       ├── memory_tools.py     # 记忆工具
│       ├── system_tools.py     # 系统信息和 Skills 列表
│       ├── database_tool.py    # 数据库交互工具（db_schema, db_query, db_connections）
│       └── todo_tools.py       # Todo 清单任务分解与进度跟踪
├── desktop/
│   └── index.html              # 单页前端（已拆分为 js/styles/libs 子目录）
├── skills/                     # 项目内 Skills（9 个 oh-my-openagent + 5 个 Superpowers）
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
| 运行时 | Python 3.9+（推荐 3.10+） |

---

## 安全边界

- 工具默认限制在当前用户工作区内，编辑工作区外文件需显式授权（点击「授权写入」按钮或设置会话工作目录）。
- 文件下载和预览只能访问当前用户工作区内的文件。
- Git 高风险命令默认不开放。
- Shell 命令禁止提权、格式化磁盘、fork bomb 等危险操作。
- 图片不会持久化保存到历史数据库。
- 长期记忆需要用户明确要求才写入，超过 10 天自动清理。
- 多用户微信 Bot token 按用户隔离存储，互不干扰。
- 公网部署必须配置强密码、固定密钥和 HTTPS。
