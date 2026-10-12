FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install -r requirements.txt \
    && groupadd --gid 10001 hive \
    && useradd --uid 10001 --gid hive --no-create-home hive
COPY backend/app/ ./backend/app/
COPY backend/alembic.ini ./backend/alembic.ini
COPY backend/migrations/ ./backend/migrations/
COPY scripts/bootstrap_db.py ./scripts/bootstrap_db.py
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
USER hive
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3)"
CMD ["python", "-m", "app"]
