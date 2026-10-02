FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖、再拷贝代码，改动业务代码时可复用依赖层缓存。
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

# 只拷贝运行所需文件；.env / config.json / data / .venv / tests 由 .dockerignore 排除，
# 密钥与配置必须通过挂载或环境变量进入容器。
COPY gateway/ ./gateway/
COPY web/ ./web/
COPY run.py manage.py setup_gateway.py smoke.py ./

ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8020

CMD ["python", "run.py"]
