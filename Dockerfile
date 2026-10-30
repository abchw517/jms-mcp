# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_VERSION=0.13.4

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM ${PYTHON_IMAGE} AS python-base

FROM python-base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON=/usr/local/bin/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /build

# Cache dependencies separately so source-only changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-editable

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python-base AS runtime
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app
# The non-editable environment is self-contained; no source or uv cache is copied.
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SERVER_HOST=0.0.0.0
USER 10001:10001
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=150s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/readyz', timeout=3)"
ENTRYPOINT ["jms-mcp"]
CMD ["serve"]
