FROM python:3.10-slim

ARG GIT_HASH=unknown
ENV FANCONTROL_GIT_HASH=${GIT_HASH}

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        git \
        gnupg \
        lm-sensors \
        smartmontools \
        util-linux && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY installer/ /app/installer/

# MODE is auto-detected: if /data/config.json doesn't exist → setup mode

# Entrypoint: on updates, copy fresh code from /repo volume into /app
RUN echo '#!/bin/bash' > /app/entrypoint.sh && \
    echo 'set -e' >> /app/entrypoint.sh && \
    echo 'echo "[entrypoint] Starting at $(date)"' >> /app/entrypoint.sh && \
    echo 'echo "[entrypoint] /repo exists: $([ -d /repo ] && echo yes || echo no)"' >> /app/entrypoint.sh && \
    echo 'echo "[entrypoint] /repo/app.py exists: $([ -f /repo/app.py ] && echo yes || echo no)"' >> /app/entrypoint.sh && \
    echo 'if [ -d "/repo" ] && [ -f "/repo/app.py" ]; then' >> /app/entrypoint.sh && \
    echo '    echo "[entrypoint] Syncing code from /repo to /app"' >> /app/entrypoint.sh && \
    echo '    rm -f /app/*.py /app/*.txt' >> /app/entrypoint.sh && \
    echo '    rm -rf /app/templates /app/static /app/core /app/server /app/agent /app/installer /app/tests' >> /app/entrypoint.sh && \
    echo '    cp -a /repo/*.py /repo/*.txt /repo/Dockerfile /repo/docker-compose.yml /app/ 2>/dev/null || true' >> /app/entrypoint.sh && \
    echo '    cp -a /repo/templates /repo/static /app/ 2>/dev/null || true' >> /app/entrypoint.sh && \
    echo '    for dir in core server agent installer tests; do' >> /app/entrypoint.sh && \
    echo '        [ -d "/repo/$dir" ] && cp -a "/repo/$dir" /app/ 2>/dev/null || true' >> /app/entrypoint.sh && \
    echo '    done' >> /app/entrypoint.sh && \
    echo '    echo "[entrypoint] Sync complete. core/state.py version: $(grep CONFIG_VERSION /app/core/state.py 2>/dev/null || echo '\''NOT FOUND'\'')"' >> /app/entrypoint.sh && \
    echo '    echo "[entrypoint] main.js version: $(grep '\''main.js?v='\'' /app/templates/index.html 2>/dev/null || echo '\''NOT FOUND'\'')"' >> /app/entrypoint.sh && \
    echo 'else' >> /app/entrypoint.sh && \
    echo '    echo "[entrypoint] SKIP sync — /repo or /repo/app.py not found"' >> /app/entrypoint.sh && \
    echo 'fi' >> /app/entrypoint.sh && \
    echo 'exec "$@"' >> /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 5059
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "app.py"]
