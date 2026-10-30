FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE requirements.lock ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir=/wheels -r requirements.lock .

FROM python:3.12-slim
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels jms-mcp && rm -rf /wheels
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SERVER_HOST=0.0.0.0
USER 10001:10001
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=150s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/readyz', timeout=3)"
ENTRYPOINT ["jms-mcp"]
CMD ["serve"]
