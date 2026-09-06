FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive
# Mark that we're running in Docker (used by scrapers for environment-specific behavior)
ENV RUNNING_IN_DOCKER=true

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY . .
CMD ["python", "main.py"]
