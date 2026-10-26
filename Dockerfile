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

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    SI_PAYPAL_MCP_COMMAND="node /opt/paypal-mcp/node_modules/@paypal/mcp/dist/index.js"

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
