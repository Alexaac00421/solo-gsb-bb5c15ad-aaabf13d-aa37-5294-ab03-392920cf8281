FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

# 依赖安装（单独一层以利用构建缓存）
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 应用代码
COPY app ./app

# 数据目录（SQLite 库、原图、发布成品），容器启动时由应用自动初始化
ENV DATA_DIR=/data \
    MAX_UPLOAD_MB=25
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
