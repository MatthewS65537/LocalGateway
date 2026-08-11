# LocalGateway — self-hosted OpenAI-compatible token gateway.
#
# Build:  docker build -t localgateway .
# Run:    docker run -p 3456:3456 -v $(pwd)/config.json:/app/config.json \
#         -v lg-data:/app/data localgateway
#
# Security: bind to 0.0.0.0 in the container and ALWAYS set an API key
# (server.api_key or server.api_keys in config.json) before exposing the
# port — the supervisor warns on startup if you bind non-loopback with no key.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LGW_CONFIG=/app/config.json \
    LGW_HOST=0.0.0.0 \
    LGW_PORT=3456

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Runtime data lives here (usage.db, snooze.json, config backups).
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 3456

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3456/', timeout=3)" || exit 1

CMD ["sh", "-c", "python3 -m localgateway.main --config \"$LGW_CONFIG\" --host \"$LGW_HOST\" --port \"$LGW_PORT\""]
