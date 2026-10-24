# syntax=docker/dockerfile:1
# Двухэтапная сборка: pip и его кэш остаются в первом этапе, в итоговый образ попадают
# только готовое виртуальное окружение и код. Зависимости ставятся из requirements.lock
# с проверкой SHA-256 каждого пакета (--require-hashes): подменённый пакет не установится.
#
# Базовый образ стоит закрепить по digest (python:3.12-slim-bookworm@sha256:…) —
# это делает Renovate (renovate.json в корне проекта).
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
COPY requirements.lock .
RUN /venv/bin/pip install --require-hashes --no-deps -r requirements.lock

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    HEARTBEAT_FILE=/tmp/bot-heartbeat

# Отдельный пользователь без shell и домашней папки; код принадлежит root и только читается
RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=build /venv /venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY bot ./bot

USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-m", "bot.core.heartbeat"]
CMD ["python", "-m", "bot"]
