FROM python:3.11-alpine

LABEL org.opencontainers.image.title="gygo"
LABEL org.opencontainers.image.description="光鸭云盘分享链接追更监控：丢一个分享链接进去，出新集自动转存"

WORKDIR /app

# 只拷贝运行需要的文件，主流程零第三方依赖，无需 pip install
COPY guangya.py share_gy.py monitor.py monitor_store.py app.py selftest.py index.html ./

ENV GYGO_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

VOLUME ["/data"]
EXPOSE 5099

CMD ["python3", "app.py"]
