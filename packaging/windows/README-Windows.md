# Desktop Agent for Windows（桌面智能体 · Windows 版）

Windows 桌面版以 **Electron 安装包** 形式分发：双击安装后即为原生窗口应用，**无需开浏览器、不暴露本地端口、目标机器免装 Python**。

## 交付形态

Windows 只提供 Electron 安装包这一种桌面交付物：

| 产物 | 说明 |
| --- | --- |
| `electron/dist/DesktopAgent-Setup-0.1.0.exe` | NSIS 安装包，内置 Electron 运行时、Python 后端与 Playwright Chromium |

> 早期提供过的「解压即用」版（`Start Desktop Agent.bat` + zip）已不再分发，以减少维护成本；PyInstaller 构建出的后端目录仅作为 electron-builder 的内部输入，不直接对外交付。

## 构建（开发者）

1. 构建自包含后端目录：

   ```cmd
   packaging\windows\build.cmd
   ```

   产物：`dist\windows\DesktopAgent-Windows\`（含 `DesktopAgent.exe` 与 `_internal/`），**不直接对外分发**。

2. 打包成 Electron 安装包：

   ```cmd
   packaging\windows\build-electron.cmd
   ```

   产物：`electron/dist/DesktopAgent-Setup-0.1.0.exe`。可加 `--skip-backend` 跳过后端重建，只用现有目录重新打包。

## 安装包内容

安装包内含三层，全部自包含、无需联网：

- **Electron 运行时**：提供原生窗口壳，隐藏随机端口
- **Python 后端 `DesktopAgent.exe`**：FastAPI 服务，启动后随机端口被 Electron 读取并加载
- **Playwright Chromium**：浏览器自动化工具所需，位于后端 `_internal/ms-playwright/`

## 数据存放位置

设置、会话、使用日志与工作区文件默认保存在：

- `%USERPROFILE%\.desktop_agent`
- `%USERPROFILE%\agent_workspace`

## 安全软件提示

若 Windows Defender / SmartScreen 拦截安装包或程序，选择「**更多信息**」→「**仍要运行**」即可（本包为本地构建的程序，属正常提示）。如要彻底消除提示，需对安装包进行代码签名。

## 常见问题排查

- **默认浏览器未自动打开**：这是正常的——Electron 版用内置窗口加载应用，不调用系统浏览器。
- **安装包体积较大（约 375MB）**：因为内置了 Electron 运行时、Python 后端与 Chromium，属于一次性的离线自包含包，无需运行时联网下载。
