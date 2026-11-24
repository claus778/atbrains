FROM python:3.13-slim

ARG PIP_INDEX_URL=""

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY atbrains_server /app/atbrains_server

RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" .; \
    else \
        pip install --no-cache-dir .; \
    fi

ENV ATBRAINS_HOST=0.0.0.0 \
    ATBRAINS_PORT=8787 \
    ATBRAINS_DATA_DIR=/data \
    ATBRAINS_AUTO_MIGRATE=1 \
    ATBRAINS_BACKGROUND_WORKERS=1

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); opener.open(f'http://127.0.0.1:{os.environ.get(\"ATBRAINS_PORT\", \"8787\")}/v1/health', timeout=3)" || exit 1

CMD ["atbrains-server", "serve"]
