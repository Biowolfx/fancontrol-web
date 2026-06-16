FROM python:3.10-slim

# Install tools and update smartmontools database
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        lm-sensors \
        smartmontools \
        util-linux && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user for safer container execution
RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

WORKDIR /app
RUN mkdir -p /app/data /app/templates/js && chown -R appuser:appuser /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .

USER appuser
EXPOSE 5059
CMD ["gunicorn", "-k", "eventlet", "-w", "1", "--bind", "0.0.0.0:5059", "app:app"]
