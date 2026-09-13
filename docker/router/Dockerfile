# The LiteLLM router. Every model call from every process resolves its
# `agent-*` alias here, so it starts before the agent and the agent waits on it
# only in the sense that its first call fails without it.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/llm-router/requirements.txt ./
# The [proxy] extra, on top of the pins. requirements.txt pins bare `litellm`
# because the host install happened to have the proxy's own dependencies
# already; a clean image does not, and litellm fails at startup with
# "Missing dependency No module named 'websockets'" rather than at install.
RUN pip install --no-cache-dir -r requirements.txt "litellm[proxy]==1.96.2"

COPY services/llm-router/custom_callbacks.py ./
# config.yaml is the OPERATOR's file (it holds their model pins) and is
# gitignored, so the image ships the example and compose mounts the real one
# over it when there is one.
COPY services/llm-router/config.example.yaml ./config.yaml

ENV PYTHONUNBUFFERED=1
EXPOSE 4000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=6 \
    CMD curl -fsS http://127.0.0.1:4000/health/liveliness || exit 1

CMD ["litellm", "--config", "/app/config.yaml", "--port", "4000", "--host", "0.0.0.0"]
