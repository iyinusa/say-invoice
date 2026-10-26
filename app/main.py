from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.logging_config import configure_logging, get_logger
from app.paypal import PayPalNotConfigured, close_paypal
from app.routers import health, webhooks
from app.settings import get_settings


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.env)
    # Before any ADK / google-genai code runs: they read credentials from env.
    settings.apply_google_env()
    get_logger("startup").info(
        "api_starting",
        env=settings.env,
        paypal_env=settings.paypal_env,
        paypal_configured=settings.paypal_configured,
        webhook_configured=bool(settings.paypal_webhook_id),
    )
    yield
    await close_paypal()
    get_logger("shutdown").info("api_stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.env)

    app = FastAPI(
        title="SayInvoice",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url=None if settings.is_production else "/api-docs",
        redoc_url=None,
    )

    @app.exception_handler(PayPalNotConfigured)
    async def _paypal_not_configured(_req: Request, exc: PayPalNotConfigured) -> JSONResponse:
        return JSONResponse({"detail": f"PayPal is not configured: {exc}"}, status_code=503)

    app.include_router(health.router)
    app.include_router(webhooks.router)

    @app.get("/", include_in_schema=False)
    async def index() -> dict[str, str]:
        return {"name": "SayInvoice", "tagline": "Speak the job. Get paid."}

    return app


app = create_app()
