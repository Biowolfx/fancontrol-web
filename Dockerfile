FROM python:3.10-slim
RUN apt-get update && apt-get install -y --no-install-recommends lm-sensors smartmontools util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5059
CMD ["python", "app.py"]
