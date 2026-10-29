# syntax=docker/dockerfile:1

# ---- build: resolve the exact dependency set from uv.lock into a virtualenv ----
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't invalidate this (slow) layer.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-install-project --no-editable
COPY src ./src
RUN uv sync --locked --no-editable

# ---- runtime ----
FROM python:3.12-slim
# tzdata: the monthly budget and daily limit follow TZ; libgomp1: OpenMP runtime for the image libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/drive /data/state /data/models \
    && chown -R app:app /data
COPY --from=build --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    DRIVE_ROOT=/data/drive \
    VINTED_LISTING_HOME=/data/state \
    U2NET_HOME=/data/models \
    TZ=Europe/Lisbon

USER app
WORKDIR /data
# The first start downloads the background-removal model (~1 GB) into /data/models: allow time.
HEALTHCHECK --interval=60s --timeout=15s --start-period=15m --retries=3 \
    CMD ["python", "-m", "vinted_listing", "health"]
ENTRYPOINT ["python", "-m", "vinted_listing"]
CMD ["watch"]
