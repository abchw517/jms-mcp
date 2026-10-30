# Use BuildKit's bundled Dockerfile frontend; no Docker Hub frontend image is needed.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.4-python3.11-alpine
FROM ${UV_IMAGE}

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON=/usr/local/bin/python \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Keep dependency installation independent of application source changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --no-editable

# Copy only files needed to install and run the project.
COPY pyproject.toml uv.lock README.md LICENSE main.py ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

RUN apk add --no-cache ca-certificates \
    && addgroup -S -g 10001 app \
    && adduser -S -D -H -u 10001 -G app app

ENV PATH="/app/.venv/bin:${PATH}" \
    UV_CACHE_DIR=/tmp/uv-cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SERVER_HOST=0.0.0.0
USER 10001:10001
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=150s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/readyz', timeout=3)"

# Use the installed console entry point; never sync/download packages at startup.
ENTRYPOINT ["uv", "run", "--no-sync", "main.py"]
CMD ["serve"]
