# 项目长期记忆

## 日志系统（2026-06-02）
- 统一日志模块：`agent_core/logger.py`
- 使用 `TimedRotatingFileHandler`，每 7 天滚动，保留 4 份备份
- 日志路径：`~/.desktop_agent/logs/agent.log`
- 日志级别：环境变量 `AGENT_LOG_LEVEL` 控制，默认 INFO
- 关键异常点使用 `logger.exception()` 记录堆栈
