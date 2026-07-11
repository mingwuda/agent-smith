# Desktop Agent for Windows（桌面智能体 · Windows 版）

本运行包**完全自包含**：已内置 Web 界面、智能体引擎以及全部内置技能，**目标机器无需安装 Python** 即可运行。

## 如何运行

1. 双击 `Start Desktop Agent.bat`。
2. 浏览器会自动打开 `http://127.0.0.1:8899/`。
3. 在页面右上角「设置」中配置你的模型服务商与 API Key。

## 包含内容

解压后顶层只有以下几项：

| 文件 / 目录 | 说明 |
| --- | --- |
| `Start Desktop Agent.bat` | 启动脚本（**双击这个运行**） |
| `DesktopAgent.exe` | 智能体服务端主程序 |
| `README-Windows.md` | 本说明文件 |
| `_internal/` | 运行依赖目录（**勿删、勿改**） |

`_internal/` 内已打包全部运行所需内容：Web 前端（`desktop/`）、内置技能（`skills/`）、智能体运行指引（`AGENTS.md`）、Python 运行时与第三方依赖、以及浏览器工具所需的 Chromium（`ms-playwright/`）。这些由程序自动读取，无需手动操作。

## 数据存放位置

设置、会话、使用日志与工作区文件默认保存在：

- `%USERPROFILE%\.desktop_agent`
- `%USERPROFILE%\agent_workspace`

## 安全软件提示

若 Windows Defender / SmartScreen 拦截该程序，选择「**更多信息**」→「**仍要运行**」即可（本包为本地构建的程序，属正常提示）。

## 常见问题排查

- **浏览器未自动打开**：保持控制台窗口运行，手动访问 `http://127.0.0.1:8899/`。
- **端口 8899 被占用**：编辑 `Start Desktop Agent.bat`，将 `set AGENT_PORT=8899` 改为其它空闲端口后重新启动。
