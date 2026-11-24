FROM python:3.13-slim

ARG PIP_INDEX_URL=""

WORKDIR /app
COPY server /app/server

RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" /app/server; \
    else \
        pip install --no-cache-dir /app/server; \
    fi

ENV ATBRAINS_HOST=0.0.0.0 \
    ATBRAINS_PORT=8787 \
    ATBRAINS_DATA_DIR=/data \
    ATBRAINS_AUTO_MIGRATE=1 \
    ATBRAINS_BACKGROUND_WORKERS=1

VOLUME ["/data"]
EXPOSE 8787

CMD ["atbrains-server", "serve"]
