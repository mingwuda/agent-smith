FROM python:3.12-slim-bookworm

LABEL maintainer="desktop-agent"
LABEL description="桌面 AI 智能体 — 自主完成分析、编码、搜索等任务"

# 设置工作目录
WORKDIR /app

# 安装系统依赖（lxml 需要 libxml2/libxslt）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖文件，利用 Docker 缓存层
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY agent_core/ ./agent_core/
COPY desktop/ ./desktop/
COPY skills/ ./skills/
COPY packaging/ ./packaging/

# 创建运行时目录
RUN mkdir -p /root/agent_workspace /root/.desktop_agent/logs

# 设置环境变量
ENV AGENT_HOST=0.0.0.0
ENV AGENT_PORT=8080
ENV PYTHONUNBUFFERED=1

# 暴露端口
EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1

# 启动服务
CMD ["python", "agent_core/main.py"]
