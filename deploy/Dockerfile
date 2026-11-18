FROM python:3.13-slim

WORKDIR /app
COPY server /app/server
COPY sdk/python /app/sdk/python

RUN pip install --no-cache-dir /app/sdk/python /app/server

ENV ATBRAINS_HOST=0.0.0.0 \
    ATBRAINS_PORT=8787 \
    ATBRAINS_DATA_DIR=/data \
    ATBRAINS_BACKGROUND_WORKERS=1

VOLUME ["/data"]
EXPOSE 8787

CMD ["atbrains-server", "serve"]
