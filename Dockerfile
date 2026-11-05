# Python app + a Node binary, because the agents talk to PayPal through PayPal's
# own MCP server (@paypal/mcp, a Node package). Postgres is external (Render Postgres).

# Stage 1: install the MCP server at build time, so an agent run never waits on npm.
FROM node:22-slim AS paypal-mcp
RUN npm install --omit=dev --prefix /opt/paypal-mcp @paypal/mcp@1.8.1

# Stage 2: the app. Only the node binary and the installed server come across.
FROM python:3.12-slim
COPY --from=paypal-mcp /usr/local/bin/node /usr/local/bin/node
COPY --from=paypal-mcp /opt/paypal-mcp /opt/paypal-mcp
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# Optional KokoroTTS server voice (SI_TTS_ENGINE=kokoro). Off by default: it adds
# PyTorch and a ~330 MB model. Build with `--build-arg INSTALL_TTS=true` (compose:
# SI_INSTALL_TTS=true). The phone's own voice is used whenever it isn't installed.
ARG INSTALL_TTS=false
ENV HF_HOME=/opt/hf
RUN if [ "$INSTALL_TTS" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends espeak-ng \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY pyproject.toml uv.lock .python-version ./
RUN if [ "$INSTALL_TTS" = "true" ]; then \
      uv sync --frozen --no-dev --no-install-project --extra tts; \
    else \
      uv sync --frozen --no-dev --no-install-project; \
    fi

COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    SI_PAYPAL_MCP_COMMAND="node /opt/paypal-mcp/node_modules/@paypal/mcp/dist/index.js"

# With the voice installed, download the model and every voice pack now and warm it
# once, so nothing is fetched at runtime. At startup the app preloads it from disk
# (SI_TTS_PRELOAD), and HF_HUB_OFFLINE stops Hugging Face checking for updates.
RUN if [ "$INSTALL_TTS" = "true" ]; then python -m app.voice.tts; fi
ENV HF_HUB_OFFLINE=${INSTALL_TTS}

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
