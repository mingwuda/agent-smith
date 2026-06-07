FROM python:3.12-slim-bookworm

LABEL maintainer="desktop-agent"
LABEL description="桌面 AI 智能体 — 自主完成分析、编码、搜索等任务"

# 设置工作目录
WORKDIR /app

# 切换为国内镜像源（清华）
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's/security.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources

# 安装系统依赖
#   - libxml2/libxslt1.1: lxml 解析 HTML
#   - curl + ca-certificates: web_search 的 fallback HTTP 客户端
#   - git: git_tools 版本控制
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    curl \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖文件，利用 Docker 缓存层
COPY requirements.txt .

# 安装 Python 依赖（清华源）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

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
