# DIFARE NEXUS v2 — Dockerfile para Railway
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Dependencias del sistema (mínimas, sin matplotlib en Fase 1)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Cache de pip — copiar requirements.txt antes que el código
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código y data
COPY . .

# Railway inyecta $PORT — gunicorn lo respeta vía variable
EXPOSE 8080
CMD gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers 1 \
    --threads 4 \
    --timeout 180 \
    --max-requests 500 \
    --max-requests-jitter 50 \
    --access-logfile - \
    --error-logfile -
