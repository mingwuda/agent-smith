"use strict";

// Electron 窗口壳：启动 Python 后端（FastAPI），读取其打印的监听地址，
// 在原生窗口中加载该地址。用户全程不接触浏览器、不记忆端口。
// 方案 A（隐藏端口）——前端 SPA 用相对路径调用 API，同源下零改动。

const { app, BrowserWindow, Menu } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

const repoRoot = path.resolve(__dirname, "..");

let backendChild = null;
let mainWindow = null;
let resolvedUrl = null;
let urlPromiseResolve = null;
// ponytail: 后端就绪前的等待者，避免窗口抢先 load 一个还不存在的地址
const urlReady = new Promise((resolve) => { urlPromiseResolve = resolve; });

// ---- 定位后端可执行文件 ----
// 打包态：electron-builder 把 PyInstaller 产物放到 resources/agent/DesktopAgent.exe
// 开发态：用 venv 里的 python 跑 agent_core/main.py
function findBackend() {
  if (process.env.AGENT_BACKEND_EXE && fs.existsSync(process.env.AGENT_BACKEND_EXE)) {
    return { exe: process.env.AGENT_BACKEND_EXE, isExe: true, args: [] };
  }
  const agentDir = path.join(process.resourcesPath || "", "agent");
  // Windows 打包态：DesktopAgent.exe
  const winExe = path.join(agentDir, "DesktopAgent.exe");
  if (fs.existsSync(winExe)) {
    return { exe: winExe, isExe: true, args: [] };
  }
  // macOS 打包态：one-folder 产物，可执行文件无扩展名（DesktopAgent）
  const macExe = path.join(agentDir, "DesktopAgent");
  if (fs.existsSync(macExe)) {
    return { exe: macExe, isExe: true, args: [] };
  }
  return { exe: path.join(repoRoot, "agent_core", "main.py"), isExe: false, args: [] };
}

// ---- 定位 python 解释器（仅开发态需要）----
function findPython() {
  if (process.env.AGENT_PYTHON && fs.existsSync(process.env.AGENT_PYTHON)) {
    return process.env.AGENT_PYTHON;
  }
  const candidates = [
    path.join(repoRoot, "venv", "Scripts", "python.exe"),
    path.join(repoRoot, "venv", "bin", "python"),
    path.join(repoRoot, ".venv-windows-build", "Scripts", "python.exe"),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return "python3";
}

function startBackend() {
  const backend = findBackend();
  const command = backend.isExe ? backend.exe : findPython();
  const args = backend.isExe ? backend.args : [backend.exe, ...backend.args];

  const env = {
    ...process.env,
    AGENT_HOST: "127.0.0.1",
    AGENT_PORT: "0", // 随机端口，由系统分配，启动后从 stdout 解析真实地址
  };

  backendChild = spawn(command, args, { env, stdio: ["ignore", "pipe", "pipe"] });

  let buf = "";
  const onData = (chunk) => {
    const text = chunk.toString();
    process.stdout.write(text); // 透传后端日志，便于排查
    buf += text;
    // 匹配固定前缀的监听地址行
    const m = buf.match(/AGENT_LISTEN_URL=(https?:\/\/[^\s]+)/);
    if (m && !resolvedUrl) {
      resolvedUrl = m[1];
      urlPromiseResolve(resolvedUrl);
      if (mainWindow) mainWindow.loadURL(resolvedUrl);
    }
  };

  backendChild.stdout.on("data", onData);
  backendChild.stderr.on("data", (d) => process.stderr.write(d));

  backendChild.on("exit", (code, signal) => {
    if (code && code !== 0) {
      console.error(`[electron] 后端进程退出，code=${code} signal=${signal}`);
    }
    // 后端意外退出则关闭整个应用，避免留下一个打不开的空窗口
    if (!app.isQuiting) app.quit();
  });

  backendChild.on("error", (err) => {
    console.error("[electron] 无法启动后端:", err.message);
    if (!app.isQuiting) app.quit();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: "Desktop Agent",
    webPreferences: {
      contextIsolation: true, // 前端无需 node 能力，保持隔离更安全
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "loading.html"));

  mainWindow.once("ready-to-show", () => mainWindow.show());

  // 后端就绪后再跳转到真实地址；若已就绪则立即加载
  urlReady.then((url) => {
    if (mainWindow && !resolvedUrl.startsWith("about:")) mainWindow.loadURL(url);
  });
  if (resolvedUrl) mainWindow.loadURL(resolvedUrl);

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null); // 移除默认菜单栏（File Edit View Window Help）
  createWindow();
  startBackend();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// 关闭所有窗口即退出（Windows / Linux 行为；macOS 可保持常驻由用户决定）
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

// 退出前确保后端子进程被回收，避免端口/进程残留
app.on("before-quit", () => {
  app.isQuiting = true;
  if (backendChild && !backendChild.killed) {
    try {
      backendChild.kill("SIGTERM");
    } catch (_) {
      /* ignore */
    }
  }
});
