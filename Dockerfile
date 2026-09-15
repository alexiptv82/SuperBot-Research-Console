# SuperBot backend Dockerfile (Railway-ready).
#
# Single-service target per project handoff §13.
# Frontend build is expected to be served separately (Railway `frontend` service),
# or you may add a build stage below when you’re ready.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/backend

# System deps: only what SQLite + libmagic-free QA needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY backend/ ./

# Persistent volume expected at /data (Railway persistent volume mount point).
ENV SUPERBOT_DATA_DIR=/data \
    SUPERBOT_DB_PATH=/data/superbot.db
VOLUME ["/data"]

EXPOSE 8001

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]
