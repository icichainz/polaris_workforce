# syntax=docker/dockerfile:1
# Polaris Workforce — Django office dashboard, API and worker portal (gunicorn).
# The same image runs the web server, the message scheduler and one-off manage.py
# commands; see docker/entrypoint.sh and the docker-compose.yml at the repository root.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

WORKDIR /app

# Dependencies first (cached while only the code changes); no dev tools in the image.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .

# An unprivileged user owns the writable paths. /data holds the SQLite file, uploads
# and the throttle cache (a volume); /app/staticfiles is shared with nginx (a volume).
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data/media /data/cache /app/staticfiles \
    && chown -R app:app /data /app/staticfiles \
    && chmod +x docker/entrypoint.sh docker/scheduler.sh

USER app

EXPOSE 8000

# Asks Django itself (not nginx) whether it answers; the headers stand in for the proxy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8000/api/v1/health/', headers={'Host': 'localhost', 'X-Forwarded-Proto': 'https'}), timeout=4)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
