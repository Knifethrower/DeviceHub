# DeviceHub server mode (devhubsrv.py) in a container: devices, keys and state live in /data (a volume).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client openssl procps curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY devhubd.py devhubsrv.py devhub.py ./
COPY ui ui
COPY beacon beacon
ENV DEVHUB_DATA=/data DEVHUB_PORT=8765 DEVHUB_BIND=0.0.0.0 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://127.0.0.1:8765/api/ping || exit 1
CMD ["python3", "devhubsrv.py"]
