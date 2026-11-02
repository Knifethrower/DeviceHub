# DeviceHub server mode (devhubsrv.py) in a container: devices, keys and state live in /data (a volume).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client openssl procps curl \
    && rm -rf /var/lib/apt/lists/*
# numpy and Pillow make screenshots and the live view fast (devhubd runs without them, slower)
RUN pip install --no-cache-dir numpy pillow
# plain known_hosts entries, so the hub's host-key pins stay readable
RUN printf 'Host *\n    HashKnownHosts no\n' > /etc/ssh/ssh_config.d/devicehub.conf
WORKDIR /app
COPY devhubd.py devhubsrv.py devhub.py ./
COPY ui ui
COPY beacon beacon
# what the hub copies to devices: the FPS monitor, the live-view streamers, the input helper
COPY fps fps
COPY stream stream
COPY helper helper
COPY inject_script2.txt ./
COPY docs docs
ENV DEVHUB_DATA=/data DEVHUB_PORT=8765 DEVHUB_BIND=0.0.0.0 PYTHONUNBUFFERED=1 DEVHUB_LOG_STDERR=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://127.0.0.1:8765/api/ping || exit 1
CMD ["python3", "devhubsrv.py"]
