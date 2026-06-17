FROM python:3.10-slim

ARG GIT_HASH=unknown
ENV FANCONTROL_GIT_HASH=${GIT_HASH}

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        lm-sensors \
        smartmontools \
        util-linux && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /app/data /app/templates/js /app/static/lang

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 5059
CMD ["gunicorn", "-k", "eventlet", "-w", "1", "--bind", "0.0.0.0:5059", "app:app"]
