# Navi AI Web 控制台 — 阶段 A：容器化部署（本地仍可用 python start_web_console.py）
FROM python:3.11-slim-bookworm

WORKDIR /app

# 不预装 gcc：requirements 在 linux/amd64 上多为 wheel，可避免 apt 走 Debian CDN 时出现 502。
# 若 pip 某包必须从源码编译报错，再恢复下面两行并多试几次 build：
# RUN apt-get update && apt-get install -y --no-install-recommends gcc \
#     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# 平台常注入 PORT；默认 8001 与本地习惯一致
EXPOSE 8001
CMD ["sh", "-c", "exec uvicorn web_console:app --host 0.0.0.0 --port ${PORT:-8001}"]
