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

ENV MODE=setup

# Entrypoint: on updates, copy fresh code from /repo volume into /app
COPY <<'ENTRYPOINT' /app/entrypoint.sh
#!/bin/bash
set -e
if [ -d "/repo" ] && [ -f "/repo/app.py" ]; then
    echo "[entrypoint] Syncing code from /repo to /app"
    rm -f /app/*.py /app/*.txt
    rm -rf /app/templates /app/static
    cp -a /repo/*.py /repo/*.txt /repo/Dockerfile /repo/docker-compose.yml /app/ 2>/dev/null || true
    cp -a /repo/templates /repo/static /app/ 2>/dev/null || true
fi
exec "$@"
ENTRYPOINT
RUN chmod +x /app/entrypoint.sh

EXPOSE 5059
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "app.py"]
