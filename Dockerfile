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
RUN mkdir -p /app/data /app/templates/js /app/static/lang

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 5059
CMD ["gunicorn", "-k", "eventlet", "-w", "1", "--bind", "0.0.0.0:5059", "app:app"]
